"""CLABE launcher script for Aind.Behavior.VrForaging + Physiology experiments.

Run with the generic clabe CLI, e.g.:
    clabe run main.py
    python -m clabe.cli run main.py
"""

import asyncio
import datetime
import logging
from pathlib import Path

import aind_physiology_fip.rig
from aind_behavior_curriculum import TrainerState
from aind_behavior_services.session import Session
from aind_behavior_services.utils import utcnow
from aind_behavior_vr_foraging.data_contract.utils import calculate_consumed_water
from aind_behavior_vr_foraging.rig import AindVrForagingRig
from aind_behavior_vr_foraging.task_logic import AindVrForagingTaskLogic
from clabe import aind_apps, resource_monitor, ui
from clabe.apps import AindBehaviorServicesBonsaiApp, CurriculumSettings
from clabe.launcher import Launcher, experiment
from clabe.logging import otel
from clabe.pickers import DefaultBehaviorPicker, DefaultBehaviorPickerSettings
from clabe.pickers.dataverse import DataversePicker

from common import (
    ByAnimalManipulatorModifier,
    confirm_session_info,
    run_curriculum_if_applicable,
    run_data_qc,
    run_data_transfer,
    run_fip_mapper,
    run_vr_foraging_mappers,
)

logger = logging.getLogger(__name__)

_DEFAULT_PICKER_SETTINGS = DefaultBehaviorPickerSettings(
    config_library_dir=r"\\allen\aind\scratch\AindBehavior.db\AindVrForaging"
)

_FIP_PICKER_SETTINGS = DefaultBehaviorPickerSettings(
    config_library_dir=r"\\allen\aind\scratch\AindBehavior.db\AindPhysiologyFip"
)


async def _run_vr_foraging_experiment(launcher: Launcher, *, with_fip: bool) -> None:
    """Shared implementation for the ``vr-foraging`` and ``vr-foraging-fip`` experiments."""
    # Start experiment setup
    picker = DataversePicker(launcher=launcher, settings=_DEFAULT_PICKER_SETTINGS)
    fip_picker = DefaultBehaviorPicker(launcher=launcher, settings=_FIP_PICKER_SETTINGS) if with_fip else None

    # Pick and register session
    session = picker.pick_session(Session)

    # Fetch the task settings
    trainer_state, task_logic = picker.pick_trainer_state(AindVrForagingTaskLogic)

    # Fetch rig settings
    logger.info("Pick VR Foraging rig...")
    rig = picker.pick_rig(AindVrForagingRig)
    fip_rig = None
    if fip_picker is not None:
        logger.info("Pick FIP rig...")
        fip_rig = fip_picker.pick_rig(aind_physiology_fip.rig.AindPhysioFipRig)

    if not confirm_session_info(launcher, session, trainer_state):
        launcher.frontend.notify("Session information not confirmed. Aborting.", ui.MessageLevel.WARNING)
        otel.event("session-aborted")
        return

    launcher.register_session(session, rig.data_directory)

    resource_monitor.ResourceMonitor(
        constrains=[
            resource_monitor.available_storage_constraint_factory(rig.data_directory, 2e11),
        ]
    ).run()

    input_trainer_state_path = launcher.session_directory / "behavior" / "trainer_state.json"
    input_trainer_state_path.parent.mkdir(parents=True, exist_ok=True)
    input_trainer_state_path.write_text(trainer_state.model_dump_json(indent=2), encoding="utf-8")

    # Post-fetching modifications
    manipulator_modifier = ByAnimalManipulatorModifier(
        subject_db_path=picker.subject_dir / session.subject,
        model_path="manipulator.calibration.initial_position",
        model_name="manipulator_init.json",
        launcher=launcher,
    )
    manipulator_modifier.inject(rig)

    # Run the VrForaging workflow, and FIP concurrently if enabled
    bonsai_app = AindBehaviorServicesBonsaiApp(
        workflow=Path(r"./Aind.Behavior.VrForaging/src/main.bonsai"),
        executable=Path(r"./Aind.Behavior.VrForaging/.bonsai/bonsai.exe"),
        temp_directory=launcher.temp_dir,
        rig=rig,
        session=session,
        task=task_logic,
    )
    if fip_rig is not None:
        fip_app = AindBehaviorServicesBonsaiApp(
            workflow=Path(r"./Aind.Physiology.Fip/src/main.bonsai"),
            executable=Path(r"./Aind.Physiology.Fip/bonsai/bonsai.exe"),
            temp_directory=launcher.temp_dir,
            rig=fip_rig,
            session=session,
        )
        await asyncio.gather(bonsai_app.run_async(), fip_app.run_async())
    else:
        await bonsai_app.run_async()

    # Update manipulator initial position for next session
    try:
        manipulator_modifier.dump()
    except Exception as e:
        logger.error("Failed to update manipulator initial position: %s", e)
        launcher.frontend.notify(f"Failed to update manipulator position: {e}", ui.MessageLevel.WARNING)
        otel.record_exception(e)

    # Curriculum
    (
        _,
        suggestion_path,
        curriculum_settings,
    ) = await run_curriculum_if_applicable(picker, trainer_state, input_trainer_state_path, launcher)

    # Waterlog
    try:
        consumed_water = calculate_consumed_water(launcher.session_directory)
        aind_apps.WaterlogApp(
            settings=aind_apps.WaterlogSettings(
                username=session.experimenter[0] if session.experimenter else None,
                mouse_id=session.subject,
                earned_water=consumed_water,
            )
        ).run()
    except Exception as e:
        logger.error("Error while attempting to waterlog: %s", e)
        otel.record_exception(e)

    # Mappers
    run_vr_foraging_mappers(launcher, suggestion_path, curriculum_settings, utcnow())
    if with_fip:
        run_fip_mapper(launcher)
    launcher.frontend.notify("Data mapping complete.", ui.MessageLevel.SUCCESS)

    # Data QC
    run_data_qc(picker, launcher)

    # Watchdog
    launcher.copy_logs()
    run_data_transfer(picker, launcher, session)


@experiment(name="vr-foraging")
async def vr_foraging_protocol(launcher: Launcher) -> None:
    """Run VrForaging on its own, without FIP."""
    await _run_vr_foraging_experiment(launcher, with_fip=False)


@experiment(name="vr-foraging-fip")
async def vr_foraging_fip_protocol(launcher: Launcher) -> None:
    """Run VrForaging and FIP concurrently as a single combined session."""
    await _run_vr_foraging_experiment(launcher, with_fip=True)


@experiment(name="calibration")
async def calibration_protocol(launcher: Launcher) -> None:
    """Run only the VrForaging rig, for calibration purposes. No data is recorded."""
    picker = DataversePicker(launcher=launcher, settings=_DEFAULT_PICKER_SETTINGS)

    session = Session(
        subject="CALIBRATION",
        experiment="CALIBRATION",
        date=utcnow(),
        allow_dirty_repo=True,
        notes="Session for rig calibration. No actual experiment data will be recorded.",
    )

    rig = picker.pick_rig(AindVrForagingRig)
    launcher.register_session(session, rig.data_directory)

    bonsai_app = AindBehaviorServicesBonsaiApp(
        workflow=Path(r"./Aind.Behavior.VrForaging/src/main.bonsai"),
        executable=Path(r"./Aind.Behavior.VrForaging/.bonsai/bonsai.exe"),
        temp_directory=launcher.temp_dir,
        task=AindVrForagingTaskLogic(),
        rig=rig,
        session=session,
    )
    await bonsai_app.run_async()
    launcher.frontend.notify("Calibration protocol completed successfully.", ui.MessageLevel.SUCCESS)


@experiment(name="recover-session")
async def recover_session(launcher: Launcher) -> None:
    """Re-run curriculum evaluation, data mapping, QC and transfer for a session
    whose bonsai workflow(s) already completed (e.g. after a launcher crash)."""
    picker = DataversePicker(launcher=launcher, settings=_DEFAULT_PICKER_SETTINGS)
    session_path = Path(
        picker.frontend.prompt_text(ui.TextRequest(label="Enter the path to the session you want to recover:"))
    )
    if not session_path.exists():
        logger.error("Session path does not exist: %s", session_path)
        launcher.frontend.notify(f"Session path does not exist: {session_path}", ui.MessageLevel.ERROR)
        return

    session_model = Session.model_validate_json(
        (session_path / "behavior/Logs/session_input.json").read_text(encoding="utf-8")
    )
    rig_model = AindVrForagingRig.model_validate_json(
        (session_path / "behavior/Logs/rig_input.json").read_text(encoding="utf-8")
    )
    trainer_state_files = list((session_path / "behavior").glob("trainer_state*.json"))
    if trainer_state_files:
        input_trainer_state_path = trainer_state_files[0]
    else:
        raise FileNotFoundError("Trainer state file not found.")
    trainer_state = TrainerState.model_validate_json(input_trainer_state_path.read_text(encoding="utf-8"))

    launcher.register_session(session_model, rig_model.data_directory)
    # TODO we should fix this in the future to prevent us from accessing the private setter
    picker._session = session_model

    suggestion_path: Path | None = None
    curriculum_settings: CurriculumSettings | None = None

    if picker.frontend.prompt_confirm(
        ui.ConfirmRequest(
            label="Would you like to run curriculum evaluation and metadata mapping?",
            default=True,
        )
    ):
        (
            _,
            suggestion_path,
            curriculum_settings,
        ) = await run_curriculum_if_applicable(picker, trainer_state, input_trainer_state_path, launcher)

        session_end_time: datetime.datetime | None = None
        while session_end_time is None:
            try:
                s = launcher.frontend.prompt_text(
                    ui.TextRequest(
                        label="Enter the session end time in ISO format (YYYY-MM-DDTHH:MM:SSz), e.g: 2024-01-01T12:00:00Z:"
                    )
                )
                session_end_time = datetime.datetime.fromisoformat(s)
            except ValueError:
                logger.error("Invalid date format. Please enter the date in ISO format.")
                launcher.frontend.notify(
                    "Invalid date format. Please use ISO format (YYYY-MM-DDTHH:MM:SSz).",
                    ui.MessageLevel.WARNING,
                )

        run_vr_foraging_mappers(launcher, suggestion_path, curriculum_settings, session_end_time)
        run_fip_mapper(launcher)

        launcher.frontend.notify("Data mapping complete.", ui.MessageLevel.SUCCESS)
    else:
        picker.frontend.notify(
            "Curriculum evaluation and metadata mapping skipped.",
            ui.MessageLevel.WARNING,
        )

    run_data_qc(picker, launcher)
    run_data_transfer(picker, launcher, session_model)
