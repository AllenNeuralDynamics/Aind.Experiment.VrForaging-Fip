import logging
from pathlib import Path
from typing import Any, cast

import asyncio
from aind_behavior_services.rig.aind_manipulator import ManipulatorPosition
from aind_behavior_services.session import Session
from aind_behavior_services.utils import utcnow
from clabe import aind_apps, resource_monitor, ui
from clabe.apps import (
    AindBehaviorServicesBonsaiApp,
    CurriculumApp,
    CurriculumSettings,
    CurriculumSuggestion,
)
from clabe.data_transfer.aind_watchdog import (
    WatchdogDataTransferService,
    WatchdogSettings,
)
from clabe.data_transfer.robocopy import RobocopyService, RobocopySettings
from clabe.launcher import Launcher, LauncherCliArgs, experiment
from clabe.pickers import (
    ByAnimalModifier,
    DefaultBehaviorPicker,
    DefaultBehaviorPickerSettings,
)
from clabe.pickers.dataverse import DataversePicker
from contraqctor.contract.json import SoftwareEvents
from pydantic_settings import CliApp

from aind_behavior_vr_foraging import data_contract
from aind_behavior_vr_foraging.data_contract.utils import calculate_consumed_water
from aind_behavior_vr_foraging.data_mappers import DataMapperCli
from aind_behavior_vr_foraging.rig import AindVrForagingRig
from aind_behavior_vr_foraging.task_logic import AindVrForagingTaskLogic
import aind_physiology_fip.rig
from aind_physiology_fip.data_mappers import ProtoAcquisitionMapper

logger = logging.getLogger(__name__)

_DEFAULT_PICKER_SETTINGS = DefaultBehaviorPickerSettings(
    config_library_dir=r"\\allen\aind\scratch\AindBehavior.db\AindVrForaging"
)

_FIP_PICKER_SETTINGS = DefaultBehaviorPickerSettings(
    config_library_dir=r"\\allen\aind\scratch\AindBehavior.db\AindPhysiologyFip"
)


async def _run_curriculum_if_applicable(
    picker: DataversePicker, input_trainer_state_path: Path, launcher: Launcher
) -> tuple[CurriculumSuggestion | None, Path | None, CurriculumSettings | None]:
    if (
        (picker.trainer_state is None)
        or (picker.trainer_state.is_on_curriculum is False)
        or (picker.trainer_state.stage is None)
    ):
        return None, None, None
    picker.frontend.notify("Running curriculum evaluation...", ui.MessageLevel.INFO)
    settings = CurriculumSettings(
        input_trainer_state=input_trainer_state_path.resolve(),
        data_directory=launcher.session_directory,
    )
    curriculum_app = CurriculumApp(settings=settings)
    await curriculum_app.run_async()
    suggestion = curriculum_app.process_suggestion()
    suggestion_path = _dump_suggestion(suggestion, launcher.session_directory)
    picker.push_new_suggestion(suggestion.trainer_state)
    return suggestion, suggestion_path, settings


def _run_data_qc(picker: DataversePicker, launcher: Launcher) -> None:
    if not picker.frontend.prompt_confirm(
        ui.ConfirmRequest(
            label="Would you like to generate a qc report?", default=False
        )
    ):
        return
    try:
        import webbrowser

        from contraqctor.qc.reporters import HtmlReporter

        from aind_behavior_vr_foraging.data_qc.data_qc import make_qc_runner

        picker.frontend.notify("Running data QC...", ui.MessageLevel.INFO)
        vr_dataset = data_contract.dataset(launcher.session_directory)
        runner = make_qc_runner(vr_dataset)
        qc_path = launcher.session_directory / "Behavior" / "Logs" / "qc_report.html"
        reporter = HtmlReporter(output_path=qc_path)
        runner.run_all_with_progress(reporter=reporter)
        picker.frontend.notify(f"QC report saved to {qc_path}", ui.MessageLevel.SUCCESS)
        webbrowser.open(qc_path.as_uri(), new=2)
    except Exception as e:
        logger.error("Failed to run data QC: %s", e)
        picker.frontend.notify(f"Failed to run data QC: {e}", ui.MessageLevel.ERROR)


def _run_data_transfer(
    picker: DataversePicker, launcher: Launcher, session: Session
) -> None:
    if not picker.frontend.prompt_confirm(
        ui.ConfirmRequest(label="Would you like to transfer data?", default=True)
    ):
        picker.frontend.notify("Data transfer skipped.", ui.MessageLevel.WARNING)
        return

    watchdog_settings = WatchdogSettings()
    watchdog_settings.destination = (
        Path(watchdog_settings.destination) / session.subject
    )

    # Immediate robocopy to move behavior data off the rig before triggering watchdog.
    try:
        RobocopyService(
            source=launcher.session_directory,
            settings=RobocopySettings(
                delete_src=False,
                destination=Path(watchdog_settings.destination)
                / launcher.session_directory.name,
                exclude_dirs=["behavior-videos"],
            ),
        ).transfer()
    except Exception as e:
        logger.error("Initial data transfer failed: %s", e)
        picker.frontend.notify(
            f"Initial data transfer failed: {e}", ui.MessageLevel.ERROR
        )

    WatchdogDataTransferService(
        source=launcher.session_directory,
        settings=watchdog_settings,
        session=session,
    ).transfer()


@experiment()
async def aind_experiment_protocol(launcher: Launcher) -> None:
    # Start experiment setup
    picker = DataversePicker(launcher=launcher, settings=_DEFAULT_PICKER_SETTINGS)
    fip_picker = DefaultBehaviorPicker(launcher=launcher, settings=_FIP_PICKER_SETTINGS)

    # Pick and register session
    session = picker.pick_session(Session)

    # Fetch the task settings
    trainer_state, task_logic = picker.pick_trainer_state(AindVrForagingTaskLogic)

    # Fetch rig settings
    logger.info("Pick VR Foraging rig...")
    rig = picker.pick_rig(AindVrForagingRig)
    logger.info("Pick FIP rig...")
    fip_rig = fip_picker.pick_rig(aind_physiology_fip.rig.AindPhysioFipRig)

    launcher.register_session(session, rig.data_directory)

    resource_monitor.ResourceMonitor(
        constrains=[
            resource_monitor.available_storage_constraint_factory(
                rig.data_directory, 2e11
            ),
        ]
    ).run()

    input_trainer_state_path = (
        launcher.session_directory / "behavior" / "trainer_state.json"
    )
    input_trainer_state_path.parent.mkdir(parents=True, exist_ok=True)
    input_trainer_state_path.write_text(
        trainer_state.model_dump_json(indent=2), encoding="utf-8"
    )

    # Post-fetching modifications
    manipulator_modifier = ByAnimalManipulatorModifier(
        subject_db_path=picker.subject_dir / session.subject,
        model_path="manipulator.calibration.initial_position",
        model_name="manipulator_init.json",
        launcher=launcher,
    )
    manipulator_modifier.inject(rig)

    # Run both workflows concurrently
    bonsai_app = AindBehaviorServicesBonsaiApp(
        workflow=Path(r"./Aind.Behavior.VrForaging/src/main.bonsai"),
        executable=Path(r"./Aind.Behavior.VrForaging/.bonsai/bonsai.exe"),
        temp_directory=launcher.temp_dir,
        rig=rig,
        session=session,
        task=task_logic,
    )
    fip_app = AindBehaviorServicesBonsaiApp(
        workflow=Path(r"./Aind.Physiology.Fip/src/main.bonsai"),
        executable=Path(r"./Aind.Physiology.Fip/bonsai/bonsai.exe"),
        temp_directory=launcher.temp_dir,
        rig=fip_rig,
        session=session,
    )
    await asyncio.gather(bonsai_app.run_async(), fip_app.run_async())

    # Update manipulator initial position for next session
    try:
        manipulator_modifier.dump()
    except Exception as e:
        logger.error("Failed to update manipulator initial position: %s", e)
        launcher.frontend.notify(
            f"Failed to update manipulator position: {e}", ui.MessageLevel.WARNING
        )

    # Curriculum
    (
        suggestion,
        suggestion_path,
        curriculum_settings,
    ) = await _run_curriculum_if_applicable(picker, input_trainer_state_path, launcher)

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

    # VR Foraging mappers
    assert launcher.repository.working_tree_dir is not None

    launcher.frontend.notify("Running data mappers...", ui.MessageLevel.INFO)
    DataMapperCli(
        data_path=launcher.session_directory,
        repository_path=Path(launcher.repository.working_tree_dir)
        / "Aind.Behavior.VrForaging",
        curriculum_suggestion=suggestion_path,
        curriculum_repository_path=curriculum_settings.project_directory
        if curriculum_settings
        else None,
        session_end_time=utcnow(),
    ).cli_cmd()

    # FIP mapper
    fip_extracted = ProtoAcquisitionMapper(launcher.session_directory).map()
    (launcher.session_directory / "fip.json").write_text(
        fip_extracted.model_dump_json(indent=2), encoding="utf-8"
    )
    launcher.frontend.notify("Data mapping complete.", ui.MessageLevel.SUCCESS)

    # Data QC
    _run_data_qc(picker, launcher)

    # Watchdog
    launcher.copy_logs()
    _run_data_transfer(picker, launcher, session)


@experiment()
async def recover_session(launcher: Launcher) -> None:
    import datetime

    picker = DataversePicker(launcher=launcher, settings=_DEFAULT_PICKER_SETTINGS)
    session_path = Path(
        picker.frontend.prompt_text(
            ui.TextRequest(label="Enter the path to the session you want to recover:")
        )
    )
    if not session_path.exists():
        logger.error("Session path does not exist: %s", session_path)
        launcher.frontend.notify(
            f"Session path does not exist: {session_path}", ui.MessageLevel.ERROR
        )
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

    launcher.register_session(session_model, rig_model.data_directory)

    suggestion: CurriculumSuggestion | None = None
    suggestion_path: Path | None = None
    curriculum_settings: CurriculumSettings | None = None

    if picker.frontend.prompt_confirm(
        ui.ConfirmRequest(
            label="Would you like to run curriculum evaluation and metadata mapping?",
            default=True,
        )
    ):
        (
            suggestion,
            suggestion_path,
            curriculum_settings,
        ) = await _run_curriculum_if_applicable(
            picker, input_trainer_state_path, launcher
        )

        assert launcher.repository.working_tree_dir is not None

        session_end_time: datetime.datetime | None = None
        while session_end_time is None:
            try:
                s = launcher.frontend.prompt_text(
                    ui.TextRequest(
                        label="Enter the session end time in ISO format (YYYY-MM-DDTHH:MM:SSz):"
                    )
                )
                session_end_time = datetime.datetime.fromisoformat(s)
            except ValueError:
                logger.error(
                    "Invalid date format. Please enter the date in ISO format."
                )
                launcher.frontend.notify(
                    "Invalid date format. Please use ISO format (YYYY-MM-DDTHH:MM:SSz).",
                    ui.MessageLevel.WARNING,
                )

        launcher.frontend.notify("Running data mappers...", ui.MessageLevel.INFO)
        DataMapperCli(
            data_path=launcher.session_directory,
            repository_path=Path(launcher.repository.working_tree_dir)
            / "Aind.Behavior.VrForaging",
            curriculum_suggestion=suggestion_path,
            curriculum_repository_path=curriculum_settings.project_directory
            if curriculum_settings
            else None,
            session_end_time=session_end_time,
        ).cli_cmd()

        # FIP mapper
        try:
            fip_extracted = ProtoAcquisitionMapper(launcher.session_directory).map()
            (launcher.session_directory / "fip.json").write_text(
                fip_extracted.model_dump_json(indent=2), encoding="utf-8"
            )
        except Exception as e:
            logger.error("Failed to run FIP mapper: %s", e)
            launcher.frontend.notify(f"FIP mapper failed: {e}", ui.MessageLevel.WARNING)

        launcher.frontend.notify("Data mapping complete.", ui.MessageLevel.SUCCESS)
    else:
        picker.frontend.notify(
            "Curriculum evaluation and metadata mapping skipped.",
            ui.MessageLevel.WARNING,
        )

    _run_data_qc(picker, launcher)
    _run_data_transfer(picker, launcher, session_model)


def _dump_suggestion(suggestion: CurriculumSuggestion, session_directory: Path) -> Path:
    path = session_directory / "Behavior" / "Logs" / "suggestion.json"
    logger.info("Dumping curriculum suggestion to: %s", path)
    with open(path, "w", encoding="utf-8") as f:
        f.write(suggestion.model_dump_json(indent=2))
    return path


class ByAnimalManipulatorModifier(ByAnimalModifier[AindVrForagingRig]):
    """Modifier to set and update manipulator initial position based on animal-specific data."""

    def __init__(
        self,
        subject_db_path: Path,
        model_path: str,
        model_name: str,
        *,
        launcher: Launcher,
        **kwargs,
    ) -> None:
        super().__init__(subject_db_path, model_path, model_name, **kwargs)
        self._launcher = launcher

    def _process_before_dump(self) -> ManipulatorPosition:
        _dataset = data_contract.dataset(self._launcher.session_directory)
        manipulator_parking_position: SoftwareEvents = cast(
            SoftwareEvents,
            _dataset["Behavior"]["SoftwareEvents"]["SpoutParkingPositions"].load(),
        )
        data: dict[str, Any] = manipulator_parking_position.data.iloc[0]["data"][
            "ResetPosition"
        ]
        return ManipulatorPosition.model_validate(data)


class ClabeCli(LauncherCliArgs):
    def cli_cmd(self):
        launcher = Launcher(settings=self)
        launcher.run_experiment(aind_experiment_protocol)
        return None


def main() -> None:
    CliApp().run(ClabeCli)


if __name__ == "__main__":
    main()
