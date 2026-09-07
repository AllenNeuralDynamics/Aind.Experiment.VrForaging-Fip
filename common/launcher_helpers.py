import datetime
import logging
from pathlib import Path
from typing import Any, cast

from aind_behavior_curriculum import TrainerState
from aind_behavior_services.rig.aind_manipulator import ManipulatorPosition
from aind_behavior_services.session import Session
from aind_behavior_vr_foraging import data_contract
from aind_behavior_vr_foraging.data_mappers import DataMapperCli
from aind_behavior_vr_foraging.rig import AindVrForagingRig
from aind_physiology_fip.data_mappers import ProtoAcquisitionMapper
from clabe import ui
from clabe.apps import CurriculumApp, CurriculumSettings, CurriculumSuggestion
from clabe.data_transfer.aind_watchdog import (
    WatchdogDataTransferService,
    WatchdogSettings,
)
from clabe.data_transfer.robocopy import RobocopyService, RobocopySettings
from clabe.launcher import Launcher
from clabe.logging import otel
from clabe.pickers import ByAnimalModifier
from clabe.pickers.dataverse import DataversePicker
from contraqctor.contract.json import SoftwareEvents

logger = logging.getLogger(__name__)


def _dump_suggestion(suggestion: CurriculumSuggestion, session_directory: Path) -> Path:
    path = session_directory / "Behavior" / "Logs" / "suggestion.json"
    logger.info("Dumping curriculum suggestion to: %s", path)
    with open(path, "w", encoding="utf-8") as f:
        f.write(suggestion.model_dump_json(indent=2))
    return path


async def run_curriculum_if_applicable(
    picker: DataversePicker,
    trainer_state: TrainerState | None,
    input_trainer_state_path: Path,
    launcher: Launcher,
) -> tuple[CurriculumSuggestion | None, Path | None, CurriculumSettings | None]:
    if (trainer_state is None) or (trainer_state.is_on_curriculum is False) or (trainer_state.stage is None):
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


def confirm_session_info(launcher: Launcher, session: Session, trainer_state: TrainerState) -> bool:
    summary = {
        "Mouse": session.subject,
        "Experimenter": ", ".join(session.experimenter) if session.experimenter else None,
        "Curriculum": trainer_state.curriculum.name if trainer_state and trainer_state.curriculum else None,
        "Stage": trainer_state.stage.name if trainer_state and trainer_state.stage else None,
    }
    return launcher.frontend.prompt_read_only_table(
        ui.ReadOnlyTable.from_object(
            summary,
            title="Confirm Session Information",
            prompt="Is this information correct?",
        )
    )


def run_data_qc(picker: DataversePicker, launcher: Launcher) -> None:
    if not picker.frontend.prompt_confirm(
        ui.ConfirmRequest(label="Would you like to generate a qc report?", default=False)
    ):
        return
    try:
        import webbrowser

        from aind_behavior_vr_foraging.data_qc.data_qc import make_qc_runner
        from contraqctor.qc.reporters import HtmlReporter

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
        otel.record_exception(e)

    fib_dir = launcher.session_directory / "fib"
    if not fib_dir.exists():
        return
    try:
        from aind_physiology_fip.data_qc import DataQcCli

        picker.frontend.notify("Running FIP data QC...", ui.MessageLevel.INFO)
        qc_assets_dir = launcher.session_directory / "Behavior" / "Logs" / "fip_qc_assets"
        for epoch in sorted(fib_dir.glob("fip_*")):
            DataQcCli(data_path=epoch, asset_path=qc_assets_dir).cli_cmd()
        picker.frontend.notify(f"FIP QC assets saved to {qc_assets_dir}", ui.MessageLevel.SUCCESS)
    except Exception as e:
        logger.error("Failed to run FIP data QC: %s", e)
        picker.frontend.notify(f"Failed to run FIP data QC: {e}", ui.MessageLevel.ERROR)
        otel.record_exception(e)


def run_data_transfer(picker: DataversePicker, launcher: Launcher, session: Session) -> None:
    if not picker.frontend.prompt_confirm(ui.ConfirmRequest(label="Would you like to transfer data?", default=True)):
        picker.frontend.notify("Data transfer skipped.", ui.MessageLevel.WARNING)
        return

    watchdog_settings = WatchdogSettings()
    watchdog_settings.destination = Path(watchdog_settings.destination) / session.subject

    # Immediate robocopy to move behavior data off the rig before triggering watchdog.
    try:
        RobocopyService(
            source=launcher.session_directory,
            settings=RobocopySettings(
                delete_src=False,
                destination=Path(watchdog_settings.destination) / launcher.session_directory.name,
                exclude_dirs=["behavior-videos", "fib"],
            ),
        ).transfer()
    except Exception as e:
        logger.error("Initial data transfer failed: %s", e)
        picker.frontend.notify(f"Initial data transfer failed: {e}", ui.MessageLevel.ERROR)
        otel.record_exception(e)

    WatchdogDataTransferService(
        source=launcher.session_directory,
        settings=watchdog_settings,
        session=session,
    ).transfer()


def run_vr_foraging_mappers(
    launcher: Launcher,
    suggestion_path: Path | None,
    curriculum_settings: CurriculumSettings | None,
    session_end_time: datetime.datetime,
) -> None:
    assert launcher.repository.working_tree_dir is not None
    repository_root = Path(launcher.repository.working_tree_dir)
    # Standalone VrForaging checkouts have the package at the repo root; when
    # this repo composes VrForaging as a submodule it lives one level deeper.
    vr_foraging_repository_path = (
        repository_root / "Aind.Behavior.VrForaging"
        if (repository_root / "Aind.Behavior.VrForaging").exists()
        else repository_root
    )

    launcher.frontend.notify("Running data mappers...", ui.MessageLevel.INFO)
    DataMapperCli(
        data_path=launcher.session_directory,
        repository_path=vr_foraging_repository_path,
        curriculum_suggestion=suggestion_path,
        curriculum_repository_path=curriculum_settings.project_directory if curriculum_settings else None,
        session_end_time=session_end_time,
    ).cli_cmd()


def run_fip_mapper(launcher: Launcher) -> None:
    if not (launcher.session_directory / "fib").exists():
        logger.info(
            "No 'fib' folder found under %s; skipping FIP mapper.",
            launcher.session_directory,
        )
        return
    fip_extracted = ProtoAcquisitionMapper(launcher.session_directory).map()
    (launcher.session_directory / "fip.json").write_text(fip_extracted.model_dump_json(indent=2), encoding="utf-8")


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
        data: dict[str, Any] = manipulator_parking_position.data.iloc[-1]["data"]["ResetPosition"]
        return ManipulatorPosition.model_validate(data)
