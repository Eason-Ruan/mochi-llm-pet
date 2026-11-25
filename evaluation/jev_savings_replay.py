"""Replay a frozen corpus of ordinary focus ticks and matched wake controls."""

from __future__ import annotations

import argparse
from dataclasses import dataclass, replace
from datetime import timedelta
from hashlib import blake2s
import json
from pathlib import Path

from deskpet.observation_v2.vision import validated_scene_elements
from evaluation.jev_shadow_replay import BYPASS, MAY_DEFER, NEEDS_SYSTEM2, START, Case, _case, _print_row, replay


@dataclass(frozen=True)
class Scene:
    kind: str
    summary: str
    elements: tuple[str, ...]

    def __post_init__(self):
        if validated_scene_elements(self.elements) != self.elements:
            raise ValueError(f"Invalid scene elements: {self.elements}")


REPORT = Scene("work", "实验报告的结果章节已展开，正文和表格在同一页。",
               ("document editor", "document outline", "document table"))
REPORT_CHART = Scene("work", "实验报告的结果章节显示误差折线图与图注。",
                     ("document editor", "document outline", "embedded chart"))
SOURCE_CHART = Scene("work", "电子表格显示实验测量值及同一组数据的误差折线图。",
                     ("spreadsheet grid", "spreadsheet chart", "sheet tabs"))
REPORT_REVIEW = Scene("work", "实验报告的误差折线图旁显示对图注的批注。",
                      ("document editor", "embedded chart", "comments panel"))
EDITOR = Scene("work", "编辑器显示网页导航组件，侧边栏定位到样式文件。",
               ("code editor", "file explorer"))
DIFF = Scene("work", "编辑器显示网页导航的对比视图，改动集中在窄屏断点。",
             ("code editor", "diff view", "source control panel"))
TESTS = Scene("work", "编辑器旁的测试面板显示同一导航组件的窄屏布局检查。",
              ("code editor", "test results panel", "terminal"))
NOTES = Scene("study", "课程笔记显示线性回归的残差图与推导提纲。",
              ("document editor", "document outline", "embedded chart"))
LECTURE = Scene("study", "课程讲解视频显示线性回归的残差图和推导字幕。",
                ("lecture video", "playback timeline", "subtitles"))
EXERCISE = Scene("study", "课程练习的笔记本显示残差分析单元格与残差图。",
                 ("notebook cells", "plot preview"))
GAME = Scene("leisure", "全屏游戏正在显示卡丁车比赛画面，画面下方是游戏控制。",
             ("game scene", "game controls"))
CLIP = Scene("leisure", "普通视频播放器显示综艺节目的舞台和播放进度。",
             ("video player", "playback timeline"))


@dataclass(frozen=True)
class Focus:
    title: str
    goal: str


REPORT_FOCUS = Focus("整理实验报告的结果章节", "写报告")
CODE_FOCUS = Focus("完善网页导航的窄屏布局", "写代码")
COURSE_FOCUS = Focus("复习线性回归课程", "学习课程")
CADENCE = 30


def _fixture(case_id, scenario, tick, expected, annotation, frames, *, focus=None,
             pending=None, reminder=False, auto_adjust=True):
    # Keep human-readable replay labels out of the event sent to System 2.
    opaque_id = blake2s(case_id.encode(), digest_size=6).hexdigest()
    samples = [(offset, scene.kind, scene.summary) for offset, scene in frames]
    case = _case(opaque_id, scenario, tick, expected, annotation, samples, focus=focus is not None,
                 pending=pending, reminder=reminder,
                 system2_case="elapsed_and_reminder" if reminder else "quiet_tick")
    context = case.context
    context["enabled_settings"]["auto_adjust"] = auto_adjust
    if focus is not None:
        phase = context["current_phase"]
        started = START - timedelta(minutes=10)
        phase.update(title=focus.title, goal=focus.goal, begin_at=started.isoformat(),
                     phase_due_at=(started + timedelta(seconds=phase["planned_duration_s"])).isoformat())
    elements = {scene.summary: list(scene.elements) for _, scene in frames}
    observations = [frame for segment in context["history_1h"]["segments"]
                    for frame in segment["visual_observations"]]
    for frame in [*observations, context["last_observed_activity"]]:
        frame["scene_elements"] = elements[frame["summary"]]
    return replace(case, id=case_id)


def _prior(scene):
    return [(offset, scene) for offset in range(-305, -35, CADENCE)]


def cases() -> tuple[Case, ...]:
    """Labels and captures are fixed before any call to a model."""
    report = [*_prior(REPORT), (-35, REPORT_CHART), (-5, REPORT_CHART)]
    report_review = [*report, (25, SOURCE_CHART), (55, REPORT_REVIEW)]
    report_game = [*report_review, (85, GAME), (115, GAME)]
    code = [*_prior(EDITOR), (-35, DIFF), (-5, DIFF)]
    code_tests = [*code, (25, DIFF), (55, TESTS)]
    course = [*_prior(NOTES), (-35, LECTURE), (-5, LECTURE)]
    course_exercise = [*course, (25, LECTURE), (55, EXERCISE)]
    return (
        _fixture("report-results-chart", "report-review", 0, MAY_DEFER,
                 "A running report timer covers the transition from the results table to its chart.",
                 report, focus=REPORT_FOCUS),
        _fixture("report-source-and-review", "report-review", 1, MAY_DEFER,
                 "The source chart and the report review concern the same results during the running timer.",
                 report_review, focus=REPORT_FOCUS),
        _fixture("report-racing-game", "report-review", 2, NEEDS_SYSTEM2,
                 "Two current captures 30 seconds apart show the same game during the report timer.",
                 report_game, focus=REPORT_FOCUS),
        _fixture("report-pause-card-painted", "report-review", 3, MAY_DEFER,
                 "The pause card has been painted for the same scene and is awaiting a response.",
                 [*report_game, (145, GAME), (175, GAME)], focus=REPORT_FOCUS, pending="phase.pause"),
        _fixture("editor-layout-diff", "editor-layout", 0, MAY_DEFER,
                 "The navigation layout and its diff remain within the running coding timer.",
                 code, focus=CODE_FOCUS),
        _fixture("editor-layout-tests", "editor-layout", 1, MAY_DEFER,
                 "The editor moves from the layout diff to its corresponding test panel.",
                 code_tests, focus=CODE_FOCUS),
        _fixture("course-lecture", "course-review", 0, MAY_DEFER,
                 "The lecture on the same subject follows the course notes during a study timer.",
                 course, focus=COURSE_FOCUS),
        _fixture("course-notebook-exercise", "course-review", 1, MAY_DEFER,
                 "The same material moves from the lecture to a course notebook exercise.",
                 course_exercise, focus=COURSE_FOCUS),
        _fixture("course-variety-clip", "course-review", 2, NEEDS_SYSTEM2,
                 "Two current captures 30 seconds apart show a variety clip after the course exercise.",
                 [*course_exercise, (85, CLIP), (115, CLIP)], focus=COURSE_FOCUS),
        _fixture("editor-layout-no-timer", "editor-no-timer", 0, NEEDS_SYSTEM2,
                 "The same sustained editor evidence has no timer and focus suggestions are enabled.", code),
        _fixture("editor-layout-suggestions-disabled", "editor-disabled", 0, MAY_DEFER,
                 "Focus suggestions are disabled and there is no due reminder.", code, auto_adjust=False),
        _fixture("report-with-reminder", "report-reminder", 0, BYPASS,
                 "The report is still in scope but a reminder window opens with the tick.",
                 report, focus=REPORT_FOCUS, reminder=True),
    )


def main():
    corpus = cases()
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--jev", choices=("off", "fixture", "real"), default="fixture")
    parser.add_argument("--system2-settings", type=Path,
                        help="Read-only settings with a verified Bailian route for the same synthetic ticks")
    parser.add_argument("--case", choices=[case.id for case in corpus], action="append",
                        help="Select an entire scenario to preserve the earlier ticks")
    parser.add_argument("--output", type=Path, required=True, help="Write a new sanitized JSON report")
    args = parser.parse_args()
    scenarios = {case.scenario for case in corpus if case.id in (args.case or ())}
    selected = [case for case in corpus if not scenarios or case.scenario in scenarios]
    with args.output.open("x", encoding="utf-8") as output:
        report = replay(selected, args.jev, system2_settings=args.system2_settings, progress=_print_row)
        report["summary"]["dataset"] = "synthetic_savings_v1"
        json.dump(report, output, ensure_ascii=False, indent=2, allow_nan=False)
        output.write("\n")


if __name__ == "__main__":
    main()
