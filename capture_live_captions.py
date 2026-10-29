from __future__ import annotations

import argparse
import difflib
import os
import re
import sys
import time
import textwrap
from collections.abc import Iterable
from pathlib import Path

import uiautomation as auto


DEFAULT_OUTPUT = Path(__file__).resolve().parent / "live_captions.txt"
DEFAULT_WINDOW_HINT = "Live captions"
DEFAULT_POLL_SECONDS = 0.8
DEFAULT_STABLE_SECONDS = 1.4
DEFAULT_LINE_WIDTH = 100
DEFAULT_RECENT_CHARS = 1500
DEFAULT_BACKGROUND_CHARS = 12000
MIN_WRITE_CHARS = 18
MIN_OVERLAP_CHARS = 24
IGNORED_EXACT_LINES = {
    "live captions",
    "desktopwindowxamlsource",
    "settings",
    "close",
    "popup",
    "popuphost",
    "popuphost popup",
}

# Phrases that often repeat when a looping video or caption buffer restarts.
LOOP_RESTART_ANCHORS = (
    "hello, welcome to magenta",
    "hello welcome to magenta",
    "and co ceos of magenta",
)

# System status line from Live Captions; do not commit as speech.
IGNORED_COMMIT_SUBSTRINGS = (
    "ready to show live captions",
    "popuphost",
)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Capture Windows 11 Live Captions text into a file."
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=DEFAULT_OUTPUT,
        help=f"Transcript file path. Defaults to {DEFAULT_OUTPUT}",
    )
    parser.add_argument(
        "--window-hint",
        default=DEFAULT_WINDOW_HINT,
        help="Partial title of the Live Captions window.",
    )
    parser.add_argument(
        "--poll-seconds",
        type=float,
        default=DEFAULT_POLL_SECONDS,
        help="Polling interval in seconds.",
    )
    parser.add_argument(
        "--stable-seconds",
        type=float,
        default=DEFAULT_STABLE_SECONDS,
        help="How long caption text must stay unchanged before it is written.",
    )
    parser.add_argument(
        "--line-width",
        type=int,
        default=DEFAULT_LINE_WIDTH,
        help="Maximum text width for each saved transcript line.",
    )
    parser.add_argument(
        "--recent-chars",
        type=int,
        default=DEFAULT_RECENT_CHARS,
        help="Approximate amount of recent committed transcript text to keep.",
    )
    parser.add_argument(
        "--background-chars",
        type=int,
        default=DEFAULT_BACKGROUND_CHARS,
        help="Approximate amount of older background transcript text to keep.",
    )
    parser.add_argument(
        "--no-reset",
        action="store_true",
        help="Do not clear transcript files at startup (default: reset all session txt files).",
    )
    return parser.parse_args()


def reset_session_transcript_files(output_path: Path) -> None:
    """Truncate full, recent, background, and legacy tmp companion files."""
    companions = list(get_companion_paths(output_path).values())
    tmp_legacy = output_path.with_suffix(output_path.suffix + ".tmp")
    paths = [*companions, tmp_legacy]
    for path in paths:
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text("", encoding="utf-8")
        except OSError as exc:
            print(f"Warning: could not reset {path}: {exc}", file=sys.stderr)


def safe_get_children(control: auto.Control) -> list[auto.Control]:
    try:
        return list(control.GetChildren())
    except Exception:
        return []


def iter_controls(control: auto.Control) -> Iterable[auto.Control]:
    yield control
    for child in safe_get_children(control):
        yield from iter_controls(child)


def find_live_captions_window(window_hint: str) -> auto.Control | None:
    try:
        desktop = auto.GetRootControl()
    except Exception:
        return None

    hint = window_hint.casefold()

    for window in safe_get_children(desktop):
        try:
            name = (window.Name or "").strip()
        except Exception:
            continue

        if hint in name.casefold():
            return window

    return None


def is_caption_candidate(text: str) -> bool:
    cleaned = text.strip()
    if not cleaned:
        return False

    lowered = cleaned.casefold()
    if lowered in IGNORED_EXACT_LINES:
        return False

    # Skip icon glyphs and other UI chrome with no real text content.
    if not any(char.isalnum() for char in cleaned):
        return False

    return True


def normalize_text(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip()


def strip_ui_chrome(text: str) -> str:
    cleaned = normalize_text(text)
    if not cleaned:
        return ""
    cleaned = re.sub(r"\bpopuphost\b", "", cleaned, flags=re.IGNORECASE)
    cleaned = re.sub(r"\bpopup\b", "", cleaned, flags=re.IGNORECASE)
    return normalize_text(cleaned)


def strip_loops_against_committed(text: str, committed_joined: str) -> str:
    """Drop a replayed intro when committed text already contains that segment."""
    text = normalize_text(text)
    committed = normalize_text(committed_joined)
    if not text or not committed:
        return text

    lower = text.casefold()
    committed_lower = committed.casefold()
    best_cut: int | None = None

    for anchor in LOOP_RESTART_ANCHORS:
        anchor_lower = anchor.casefold()
        if anchor_lower not in committed_lower:
            continue

        search_from = 0
        while True:
            pos = lower.find(anchor_lower, search_from)
            if pos < 0:
                break
            search_from = pos + len(anchor_lower)
            tail_from_anchor = text[pos:]
            trim_len = compute_prefix_trim_length(tail_from_anchor, committed)
            sample = tail_from_anchor[: min(500, len(tail_from_anchor))]
            committed_sample = committed[: min(500, len(committed))]
            if trim_len >= MIN_OVERLAP_CHARS and trim_len >= len(tail_from_anchor) * 0.45:
                if best_cut is None or pos < best_cut:
                    best_cut = pos
            elif similarity_ratio(sample, committed_sample) > 0.65:
                if best_cut is None or pos < best_cut:
                    best_cut = pos

    if best_cut is not None and best_cut > 0:
        return text[:best_cut].strip()
    return text


def strip_replay_after_signoff(text: str, committed_joined: str) -> str:
    """When a looping video restarts after a sign-off, drop the replayed tail."""
    text = normalize_text(text)
    committed = normalize_text(committed_joined)
    if not text or not committed or len(committed) < 80:
        return text

    lower = text.casefold()
    for closing in ("bye, bye", "bye bye", "take care. bye", "take care bye"):
        idx = lower.find(closing)
        if idx < 0:
            continue

        after = idx + len(closing)
        tail = text[after:].strip()
        if len(tail) < MIN_OVERLAP_CHARS:
            return text

        matcher = difflib.SequenceMatcher(None, committed.casefold(), tail.casefold())
        match = matcher.find_longest_match(0, len(committed), 0, len(tail))
        if match.size >= MIN_OVERLAP_CHARS and match.size >= len(tail) * 0.30:
            return text[:after].strip()

        for anchor in LOOP_RESTART_ANCHORS:
            anchor_lower = anchor.casefold()
            if anchor_lower not in committed.casefold():
                continue
            replay_pos = tail.casefold().find(anchor_lower)
            if replay_pos < 0:
                continue
            replay_tail = tail[replay_pos:]
            if compute_prefix_trim_length(replay_tail, committed) >= MIN_OVERLAP_CHARS:
                return text[:after].strip()
            if similarity_ratio(
                replay_tail[: min(350, len(replay_tail))],
                committed[: min(350, len(committed))],
            ) > 0.55:
                return text[:after].strip()

    return text


def strip_internal_loop_restart(text: str, committed_joined: str) -> str:
    """Remove a second copy of the same intro inside one caption snapshot."""
    text = normalize_text(text)
    committed = normalize_text(committed_joined)
    if not text:
        return text

    lower = text.casefold()
    for anchor in LOOP_RESTART_ANCHORS:
        anchor_lower = anchor.casefold()
        first = lower.find(anchor_lower)
        if first < 0:
            continue
        second = lower.find(anchor_lower, first + len(anchor_lower))
        if second < 0:
            continue

        before_second = text[:second].strip()
        repeat_tail = text[second:].strip()
        first_block = text[first:second].strip()

        if committed and anchor_lower in committed.casefold():
            if similarity_ratio(repeat_tail, committed) > 0.55:
                return before_second
            if similarity_ratio(repeat_tail[: min(300, len(repeat_tail))], first_block[: min(300, len(first_block))]) > 0.60:
                return before_second

        if similarity_ratio(repeat_tail[: min(200, len(repeat_tail))], first_block[: min(200, len(first_block))]) > 0.65:
            return before_second

    return text


def collect_visible_lines(window: auto.Control) -> list[str]:
    lines: list[str] = []
    seen: set[str] = set()

    for control in iter_controls(window):
        try:
            name = (control.Name or "").strip()
        except Exception:
            continue

        if not is_caption_candidate(name):
            continue

        lowered = normalize_text(name).casefold()
        if lowered in seen:
            continue

        seen.add(lowered)
        lines.append(normalize_text(name))

    return lines


def compute_increment(previous_snapshot: str, current_snapshot: str) -> str:
    if not current_snapshot:
        return ""

    if not previous_snapshot:
        return current_snapshot

    if current_snapshot == previous_snapshot:
        return ""

    if current_snapshot.startswith(previous_snapshot):
        return current_snapshot[len(previous_snapshot) :].strip()

    if previous_snapshot in current_snapshot:
        start = current_snapshot.index(previous_snapshot) + len(previous_snapshot)
        return current_snapshot[start:].strip()

    max_overlap = min(len(previous_snapshot), len(current_snapshot))
    for overlap in range(max_overlap, 0, -1):
        if previous_snapshot[-overlap:] == current_snapshot[:overlap]:
            return current_snapshot[overlap:].strip()

    return current_snapshot


def similarity_ratio(left: str, right: str) -> float:
    if not left or not right:
        return 0.0
    return difflib.SequenceMatcher(a=left.casefold(), b=right.casefold()).ratio()


def is_likely_complete(text: str) -> bool:
    if not text:
        return False
    return text.endswith((".", "?", "!", ":", ";"))


def should_write_snapshot(snapshot: str, last_written_text: str) -> bool:
    if len(snapshot) < MIN_WRITE_CHARS:
        return False

    if snapshot == last_written_text:
        return False

    if similarity_ratio(snapshot, last_written_text) > 0.97:
        return False

    return True


def should_ignore_commit_text(text: str) -> bool:
    lower = text.casefold()
    return any(s in lower for s in IGNORED_COMMIT_SUBSTRINGS)


def trim_leading_fragment(text: str) -> str:
    return re.sub(r"^[\s.,;:!?-]+", "", text).strip()


def compute_prefix_trim_length(candidate: str, anchor: str) -> int:
    """
    How many characters at the start of candidate repeat anchor text.

    Handles pause/resume: after a break, Live Captions often shows the same
    opening lines again when speech continues.
    """
    if not candidate or not anchor:
        return 0

    cand = normalize_text(candidate)
    anch = normalize_text(anchor)
    if not cand or not anch:
        return 0

    limit = min(len(cand), len(anch))
    index = 0
    while index < limit and cand[index].casefold() == anch[index].casefold():
        index += 1
    if index >= MIN_OVERLAP_CHARS:
        return index

    matcher = difflib.SequenceMatcher(None, anch.casefold(), cand.casefold())
    best = 0
    for _anchor_pos, candidate_pos, size in matcher.get_matching_blocks():
        if candidate_pos == 0 and size > best:
            best = size
    if best >= MIN_OVERLAP_CHARS and best >= len(anch) * 0.80:
        return best

    return 0


def strip_repeated_committed_prefix(
    candidate: str, anchors: Iterable[str]
) -> str:
    cand = normalize_text(candidate)
    if not cand:
        return ""

    best_trim = 0
    for anchor in anchors:
        best_trim = max(best_trim, compute_prefix_trim_length(cand, anchor))

    if best_trim < MIN_OVERLAP_CHARS:
        return cand

    return trim_leading_fragment(cand[best_trim:])


def dedupe_anchors(
    committed_joined: str,
    committed_entries: list[tuple[str, str]] | None = None,
) -> list[str]:
    anchors: list[str] = []
    if committed_entries:
        anchors.extend(text for _, text in reversed(committed_entries))
    if committed_joined:
        anchors.append(committed_joined)
    return anchors


def dedupe_against_committed_text(
    committed_joined: str,
    text: str,
    committed_entries: list[tuple[str, str]] | None = None,
) -> str:
    """Drop text that is already present in the committed transcript."""
    if not text:
        return ""

    inc = normalize_text(text)
    if not inc:
        return ""

    committed = normalize_text(committed_joined) if committed_joined else ""
    anchors = dedupe_anchors(committed_joined, committed_entries)
    if anchors:
        inc = strip_repeated_committed_prefix(inc, anchors)
        if not inc:
            return ""

    if not committed:
        return inc

    if inc.casefold() in committed.casefold():
        return ""

    max_overlap = min(len(committed), len(inc), 2000)
    for overlap in range(max_overlap, MIN_OVERLAP_CHARS - 1, -1):
        if committed[-overlap:].casefold() == inc[:overlap].casefold():
            tail = inc[overlap:].strip()
            if not tail or tail.casefold() in committed.casefold():
                return ""
            return tail

    if len(inc) <= len(committed) and similarity_ratio(inc, committed) > 0.90:
        return ""

    if committed and len(inc) >= MIN_OVERLAP_CHARS:
        matcher = difflib.SequenceMatcher(None, committed.casefold(), inc.casefold())
        match = matcher.find_longest_match(0, len(committed), 0, len(inc))
        if match.b == 0 and match.size >= min(len(inc) * 0.55, 500):
            inc = trim_leading_fragment(inc[match.size :])
            if not inc:
                return ""

    return inc


def polish_caption_chunk(
    text: str,
    committed_joined: str,
    committed_entries: list[tuple[str, str]] | None = None,
) -> str:
    if not text:
        return ""
    text = strip_ui_chrome(text)
    text = strip_replay_after_signoff(text, committed_joined)
    text = strip_loops_against_committed(text, committed_joined)
    text = strip_internal_loop_restart(text, committed_joined)
    return dedupe_against_committed_text(committed_joined, text, committed_entries)


def extract_uncommitted_tail(
    last_full_snapshot: str,
    current_snapshot: str,
    committed_joined: str,
    committed_entries: list[tuple[str, str]] | None = None,
) -> str:
    """
    Return only caption text that is not already committed.

    Live Captions rolls its on-screen buffer, so the visible snapshot may repeat
    earlier paragraphs or show a middle slice. Compare against both the last full
    buffer and the joined committed transcript.
    """
    if not current_snapshot:
        return ""

    current = normalize_text(current_snapshot)
    committed = normalize_text(committed_joined) if committed_joined else ""

    if last_full_snapshot:
        increment = normalize_text(
            compute_increment(last_full_snapshot, current_snapshot)
        )
        if increment and increment != current:
            return polish_caption_chunk(
                increment, committed, committed_entries
            )

    if committed:
        increment = normalize_text(compute_increment(committed, current_snapshot))
        if increment:
            return polish_caption_chunk(
                increment, committed, committed_entries
            )

    if committed and current.casefold() in committed.casefold():
        return ""

    return polish_caption_chunk(current, committed, committed_entries)


def text_to_commit_on_stable(
    last_full_snapshot: str,
    current_snapshot: str,
    committed_joined: str = "",
    committed_entries: list[tuple[str, str]] | None = None,
) -> str | None:
    """
    Live Captions often keeps the last part of the previous utterance visible when
    new speech starts, so consecutive full snapshots overlap. Store only the new
    tail so full / recent / background views do not repeat the same paragraphs.
    """
    if not current_snapshot:
        return None

    if should_ignore_commit_text(current_snapshot):
        return None

    if not last_full_snapshot and not committed_joined:
        return normalize_text(current_snapshot)

    tail = extract_uncommitted_tail(
        last_full_snapshot,
        current_snapshot,
        committed_joined,
        committed_entries,
    )
    tail = normalize_text(tail)

    if len(tail) >= MIN_WRITE_CHARS:
        return tail

    if last_full_snapshot and similarity_ratio(current_snapshot, last_full_snapshot) > 0.88:
        return None

    return None


def format_entry_lines(label: str, text: str, line_width: int) -> list[str]:
    prefix = f"{label} "
    wrap_width = max(20, line_width)
    wrapped = textwrap.wrap(text, width=wrap_width) or [text]

    lines = [f"{prefix}{wrapped[0]}"]
    continuation_prefix = " " * len(prefix)
    for line in wrapped[1:]:
        lines.append(f"{continuation_prefix}{line}")

    return lines


def get_companion_paths(output_path: Path) -> dict[str, Path]:
    return {
        "full": output_path,
        "background": output_path.with_name(f"{output_path.stem}_background{output_path.suffix}"),
        "recent": output_path.with_name(f"{output_path.stem}_recent{output_path.suffix}"),
    }


def join_committed_plain(entries: list[tuple[str, str]]) -> str:
    parts = [normalize_text(text) for _, text in entries]
    parts = [p for p in parts if p]
    return "\n\n".join(parts)


def slice_recent_from_entries(
    committed_entries: list[tuple[str, str]], recent_chars: int
) -> str:
    """Build recent view from newest whole commits, not a mid-sentence char chop."""
    if not committed_entries:
        return ""

    parts: list[str] = []
    total = 0
    limit = max(1, recent_chars)

    for _, text in reversed(committed_entries):
        normalized = normalize_text(text)
        if not normalized:
            continue
        extra = len(normalized) + (1 if parts else 0)
        if parts and total + extra > limit:
            break
        parts.insert(0, normalized)
        total += extra

    return " ".join(parts)


def slice_character_windows(
    committed_entries: list[tuple[str, str]],
    joined: str,
    recent_chars: int,
    background_chars: int,
) -> tuple[str, str]:
    """
    Split flat transcript for companion files:
    - recent: newest committed chunks up to `recent_chars`.
    - background: first `background_chars` of the text before that tail.
    """
    joined = joined.strip()
    if not joined:
        return "", ""

    recent_body = slice_recent_from_entries(committed_entries, recent_chars)
    if not recent_body:
        return "", ""

    rc = max(1, recent_chars)
    bc = max(0, background_chars)

    if len(joined) <= rc:
        return "", recent_body

    recent_start = joined.rfind(recent_body)
    if recent_start < 0:
        recent_start = max(0, len(joined) - rc)

    prefix = joined[:recent_start].strip()
    background_body = prefix[:bc].strip() if bc > 0 and prefix else ""

    return background_body, recent_body


def render_plain_block(heading: str, body: str, line_width: int) -> str:
    if not body.strip():
        return ""
    lines = format_entry_lines(heading, body, line_width)
    return "\n".join(lines).rstrip() + "\n"


def render_background_view(background_body: str, line_width: int) -> str:
    if background_body.strip():
        return render_plain_block("[background — early context]", background_body, line_width)

    placeholder = (
        "(Nothing here yet — once the transcript is longer than the recent window, "
        "the start of the session appears here.)"
    )
    return render_plain_block("[background — early context]", placeholder, line_width)


def render_recent_view(recent_body: str, live_text: str, line_width: int) -> str:
    chunks: list[str] = []

    if recent_body.strip():
        chunks.append(
            render_plain_block("[recent]", recent_body, line_width)
        )

    cleaned_live = normalize_text(live_text)
    if cleaned_live:
        chunks.append(
            "\n".join(format_entry_lines("[LIVE]", cleaned_live, line_width)).rstrip() + "\n"
        )

    if not chunks:
        return ""

    return "\n\n".join(chunks).rstrip() + "\n"


def render_transcript(
    committed_entries: list[tuple[str, str]], live_text: str, line_width: int
) -> str:
    rendered_lines: list[str] = []

    for timestamp, text in committed_entries:
        rendered_lines.extend(format_entry_lines(f"[{timestamp}]", text, line_width))
        rendered_lines.append("")

    cleaned_live = normalize_text(live_text)
    if cleaned_live:
        rendered_lines.extend(format_entry_lines("[LIVE]", cleaned_live, line_width))

    if not rendered_lines:
        return ""

    return "\n".join(rendered_lines).rstrip() + "\n"


def build_transcript_views(
    output_path: Path,
    committed_entries: list[tuple[str, str]],
    live_text: str,
    line_width: int,
    recent_chars: int,
    background_chars: int,
) -> dict[Path, str]:
    companion_paths = get_companion_paths(output_path)
    joined = join_committed_plain(committed_entries)
    background_body, recent_body = slice_character_windows(
        committed_entries, joined, recent_chars, background_chars
    )

    return {
        companion_paths["full"]: render_transcript(committed_entries, live_text, line_width),
        companion_paths["background"]: render_background_view(background_body, line_width),
        companion_paths["recent"]: render_recent_view(recent_body, live_text, line_width),
    }


def write_transcript_view(output_path: Path, content: str) -> bool:
    output_path.parent.mkdir(parents=True, exist_ok=True)

    for _ in range(3):
        try:
            with output_path.open("w", encoding="utf-8") as handle:
                handle.write(content)
                handle.flush()
                os.fsync(handle.fileno())
            return True
        except PermissionError:
            time.sleep(0.15)

    return False


def main() -> int:
    args = parse_args()
    output_path = args.output.resolve()
    ignore_stale_ui = False
    stale_ui_baseline: str | None = None
    if not args.no_reset:
        reset_session_transcript_files(output_path)
        print("Session transcript files reset (empty).")
        # Live Captions keeps prior lines on screen; UI Automation would refill our
        # txt files from that stale buffer unless we wait for the visible text to change.
        ignore_stale_ui = True

    current_snapshot = ""
    snapshot_changed_at = time.monotonic()
    last_committed_snapshot = ""
    committed_entries: list[tuple[str, str]] = []
    missing_window_logged = False
    last_rendered_content: dict[Path, str] = {}
    companion_paths = get_companion_paths(output_path)

    print(f"Watching Live Captions and writing to: {output_path}")
    print(f"Looking for window title containing: {args.window_hint!r}")
    print(f"Background view: {companion_paths['background']}")
    print(f"Recent view: {companion_paths['recent']}")
    print("Press Ctrl+C to stop.")

    while True:
        try:
            window = find_live_captions_window(args.window_hint)
            if window is None:
                if not missing_window_logged:
                    print("Live Captions window not found yet.")
                    missing_window_logged = True
                time.sleep(args.poll_seconds)
                continue

            if missing_window_logged:
                print("Live Captions window found.")
                missing_window_logged = False

            current_lines = collect_visible_lines(window)
            visible_snapshot = strip_ui_chrome(" ".join(current_lines))
            now = time.monotonic()

            if ignore_stale_ui:
                if stale_ui_baseline is None:
                    stale_ui_baseline = visible_snapshot
                    print(
                        "Ignoring text already on the Live Captions screen "
                        "(will capture after new captions appear)."
                    )
                    time.sleep(args.poll_seconds)
                    continue
                if visible_snapshot == stale_ui_baseline:
                    time.sleep(args.poll_seconds)
                    continue
                ignore_stale_ui = False
                committed_entries.clear()
                last_committed_snapshot = ""
                current_snapshot = ""
                snapshot_changed_at = now
                last_rendered_content.clear()
                print("Live Captions content changed; capturing from here.")

            if visible_snapshot != current_snapshot:
                current_snapshot = visible_snapshot
                snapshot_changed_at = now
            elif current_snapshot and (now - snapshot_changed_at) >= args.stable_seconds:
                committed_joined = join_committed_plain(committed_entries)
                if should_write_snapshot(current_snapshot, last_committed_snapshot):
                    text = text_to_commit_on_stable(
                        last_committed_snapshot,
                        current_snapshot,
                        committed_joined,
                        committed_entries,
                    )
                    if text:
                        text = normalize_text(text)
                    if text and len(text) >= MIN_WRITE_CHARS:
                        committed_entries.append(
                            (time.strftime("%Y-%m-%d %H:%M:%S"), text)
                        )
                        preview = text if len(text) <= 160 else text[:157] + "..."
                        print(f"Committed: {preview}")
                        print(f"Wrote to: {output_path}")
                    last_committed_snapshot = current_snapshot
                elif is_likely_complete(current_snapshot):
                    last_committed_snapshot = current_snapshot
                elif committed_joined:
                    if not extract_uncommitted_tail(
                        last_committed_snapshot,
                        current_snapshot,
                        committed_joined,
                        committed_entries,
                    ):
                        last_committed_snapshot = current_snapshot

            committed_joined = join_committed_plain(committed_entries)
            live_preview = extract_uncommitted_tail(
                last_committed_snapshot,
                current_snapshot,
                committed_joined,
                committed_entries,
            )

            rendered_views = build_transcript_views(
                output_path,
                committed_entries,
                live_preview,
                args.line_width,
                args.recent_chars,
                args.background_chars,
            )
            for target_path, rendered_content in rendered_views.items():
                if rendered_content == last_rendered_content.get(target_path, ""):
                    continue

                if write_transcript_view(target_path, rendered_content):
                    last_rendered_content[target_path] = rendered_content
                else:
                    print(f"Write blocked: {target_path}", file=sys.stderr)

            time.sleep(args.poll_seconds)
        except KeyboardInterrupt:
            print("\nStopped.")
            return 0
        except Exception as exc:
            print(f"Capture error: {exc}", file=sys.stderr)
            time.sleep(2)


if __name__ == "__main__":
    raise SystemExit(main())
