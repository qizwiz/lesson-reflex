"""Run: python3 -m unittest discover -s tests   (standard library only; no model is called)."""
import json
import os
import subprocess
import sys
import tempfile
import time
import unittest
from concurrent.futures import ThreadPoolExecutor

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
HOOKS = os.path.join(ROOT, "hooks")
CLI = os.path.join(ROOT, "bin", "lesson")
sys.path.insert(0, HOOKS)
# The test process itself must never touch the real ~/.claude/lesson-reflex.
_HOME = tempfile.TemporaryDirectory()
os.environ["LESSON_REFLEX_HOME"] = os.path.join(_HOME.name, "home")
import ast_lessons  # noqa: E402
import capture  # noqa: E402
import failure  # noqa: E402
import hindsight  # noqa: E402
import judge  # noqa: E402
import reflex  # noqa: E402
import store  # noqa: E402

TOKEN = "ghp_" + "A1b2C3d4E5f6G7h8I9j0K1l2M3n4O5p6Q7r8"  # shaped like a key, not one


def run(script, ev, env):
    return subprocess.run([sys.executable, script], input=json.dumps(ev), capture_output=True, text=True,
                          env=env, timeout=30, check=False)


def transcript(path, rows):
    with open(path, "w") as fh:
        fh.writelines((r if isinstance(r, str) else json.dumps(r)) + "\n" for r in rows)


def said(text):
    return {"type": "assistant", "message": {"content": [{"type": "text", "text": text}]}}


def typed(text):
    return {"type": "user", "message": {"content": text}}


class Pure(unittest.TestCase):
    def test_validate(self):
        ok = {"tool": "Bash", "needle": "git push", "lesson": "x" * 25}
        self.assertEqual(store.validate(ok, []), "")
        self.assertIn("already exists", store.validate(ok, [ok]))
        self.assertIn("--tool", store.validate(dict(ok, tool="Grep"), []))
        self.assertIn("4 characters", store.validate(dict(ok, needle="ls"), []))
        self.assertIn("instead", store.validate(dict(ok, lesson="no"), []))

    def test_secret_shapes(self):
        for text in (TOKEN, f"'{TOKEN}'", f"TOKEN={TOKEN}", '"sk-ant-api03-' + "a9" * 15 + '"', "AKIA" + "B7" * 8,
                     "sk_test_" + "c4" * 12, "xoxa-" + "d5" * 10):
            self.assertTrue(store.carries_secret(text), text[:12])
        for text in ("sk-learn is a library", "AKIAisnotakey", "use ghp_ tokens, never paste one",
                     "pin flask-sqlalchemy-utils to 0.41", "use disk-usage-reporter-cli instead of du",
                     "set ASIA_PACIFIC_REGION_ENDPOINT before deploy", "sk-learn-compatible-estimator-pipelines",
                     "sk-rate-limiter-middleware-for-express", "ASIAPACIFICREGIONSOUTH2"):
            self.assertFalse(store.carries_secret(text), text)
        ok = {"tool": "Bash", "needle": "git push", "lesson": "x" * 25}
        for field in ("needle", "lesson", "source"):
            self.assertIn("credential", store.validate(dict(ok, **{field: "x" * 25 + " " + TOKEN}), []))

    def test_corrupt_line_costs_only_itself(self):
        with tempfile.TemporaryDirectory() as d:
            p = os.path.join(d, "l.jsonl")
            with open(p, "w") as fh:
                fh.write('{bad\n{"a": 1}\n\n[1]\n{"b": 2}\n')
            self.assertEqual(store.read_jsonl(p), [{"a": 1}, {"b": 2}])

    def test_mask_then_cut(self):
        self.assertEqual(store.mask("use " + TOKEN + " now"), "use <secret> now")
        self.assertEqual(store.mask("sk-learn-compatible-estimator"), "sk-learn-compatible-estimator")
        row = judge.record({"kind": "failure", "session": "F", "action": "x" * 590 + " " + TOKEN, "error": "e",
                            "program": "curl"}, "mistake")
        self.assertNotIn(TOKEN[:9], row["prompt"], "a credential straddling the 600-char cut is masked first")
        self.assertEqual((row["verdict"], row["kind"], row["program"]), ("correction", "failure", "curl"))

    def test_correction_and_hindsight_announce_once(self):
        judge.record({"session": "D", "prompt": "no, they fail", "previous": "tests pass"}, "correction")
        dup = judge.record({"kind": "hindsight", "session": "D", "reply": "tests pass", "prompt": "no, they fail",
                            "follow": "f"}, "error")
        self.assertEqual(dup["verdict"], "duplicate")
        self.assertNotIn(dup["id"], reflex.read_inbox("D"))

    def test_judge_failure_is_not_a_hindsight_error(self):
        row = judge.record({"kind": "hindsight", "session": "J", "reply": "r", "prompt": "p", "follow": "f"}, "",
                           "judge exited 1")
        self.assertEqual(row["verdict"], "judge-error")

    def test_hindsight_text_is_masked_before_cut(self):
        row = judge.record({"kind": "hindsight", "session": "M", "prompt": "p", "follow": "f",
                            "reply": "[tool Bash: curl -H " + TOKEN + "] " + "y" * 590}, "error")
        self.assertNotIn(TOKEN[:9], row["prompt"])

    def test_failure_program_is_hashed_and_masked(self):
        ev = {"tool_name": "Bash", "tool_input": {"command": TOKEN + " --help"}, "error": "e", "session_id": "TK"}
        job = failure.job_for(ev)
        with open(os.path.join(os.environ["LESSON_REFLEX_HOME"], "failures-seen.jsonl")) as fh:
            self.assertNotIn(TOKEN[:9], fh.read())
        row = judge.record(job, "mistake")
        self.assertNotIn(TOKEN[:9], json.dumps(row), "neither the action nor the program field holds the token")

    def test_failure_program_skips_wrapper_values(self):
        for cmd in ("timeout -s KILL 5 ls", "xargs -I {} ls {}", "timeout -k 3 10 ls"):
            self.assertEqual(failure.program("Bash", {"command": cmd}), "ls", cmd)

    def test_failure_program_skips_wrappers(self):
        for cmd, want in (("env -i FOO=1 make", "make"), ("sudo -u root ls", "ls"), ("time -p ls", "ls"),
                          ("{ ls; }", "ls"), ("timeout 10 pytest", "pytest"), ("nice -n 5 make", "make")):
            self.assertEqual(failure.program("Bash", {"command": cmd}), want, cmd)

    def test_failure_program_and_gating(self):
        self.assertEqual(failure.program("Bash", {"command": "cd ~/x && git -C y status"}), "git")
        self.assertEqual(failure.program("Bash", {"command": "FOO=1 python3 -c 'x'"}), "python3")
        self.assertEqual(failure.program("mcp__x__y", {}), "mcp__x__y")
        ev = {"tool_name": "Bash", "tool_input": {"command": "arc --help"}, "error": "exit 64", "session_id": "G"}
        self.assertEqual(failure.job_for(ev)["kind"], "failure")
        self.assertIsNone(failure.job_for(ev), "one judgement per program per session")
        self.assertIsNotNone(failure.job_for(dict(ev, session_id="G2")))
        self.assertIsNone(failure.job_for(dict(ev, session_id="G3", is_interrupt=True)))

    def test_ast_lessons_shipped_check(self):
        lines = [ln for ln, _ in ast_lessons.slice_into_redact(
            "def a(x):\n    return x[:9]\n\ndef b(x):\n    return mask(a(x))\n")]
        self.assertEqual(lines, [5])
        for path in ("judge.py", "failure.py", "capture.py", "store.py", "reflex.py", "hindsight.py"):
            with open(os.path.join(HOOKS, path)) as fh:
                self.assertEqual(ast_lessons.check_source(fh.read()), [], f"the plugin passes its own check: {path}")

    def test_hindsight_record_is_a_pending_correction(self):
        row = judge.record({"kind": "hindsight", "session": "H", "reply": "x" * 700 + "THE END", "prompt": "p",
                            "follow": "f"}, "error")
        self.assertEqual((row["verdict"], row["kind"]), ("correction", "hindsight"))
        self.assertTrue(row["prompt"].endswith("THE END") and len(row["prompt"]) == 600)
        fine = judge.record({"kind": "hindsight", "session": "H", "reply": "r", "prompt": "p", "follow": "f"}, "fine")
        self.assertNotIn("prompt", fine, "only an error keeps text")
        self.assertEqual(judge.parse("Error.", judge.HINDSIGHT_VERDICTS), "error")
        self.assertEqual(judge.parse("correction", judge.HINDSIGHT_VERDICTS), "")

    def test_parse_verdict_reads_first_word_only(self):
        self.assertEqual(judge.parse("Correction."), "correction")
        self.assertEqual(judge.parse("**correction**"), "correction")
        self.assertEqual(judge.parse("correction: the user objects"), "correction")
        self.assertEqual(judge.parse("request"), "request")
        self.assertEqual(judge.parse("Not a correction, this is a request"), "")
        self.assertEqual(judge.parse("correction\u2014the user objects"), "correction")
        self.assertEqual(judge.parse("I cannot tell"), "")

    def test_judge_is_sealed(self):
        argv = judge.judge_argv()
        for flag in ("--no-session-persistence", "--safe-mode", "--strict-mcp-config", "--tools"):
            self.assertIn(flag, argv)
        self.assertEqual(argv[argv.index("--tools") + 1], "")

    def test_prompt_rides_stdin_not_argv(self):
        seen = {}

        def fake(argv, **kw):
            seen.update(argv=argv, stdin=kw.get("input"))
            return subprocess.CompletedProcess(argv, 0, "request", "")

        os.environ.pop("LESSON_REFLEX_JUDGE", None)
        judge.run_judge({"previous": "PREV-SECRET-TEXT", "prompt": "PROMPT-TEXT"}, run=fake)
        self.assertNotIn("PROMPT-TEXT", " ".join(seen["argv"]), "visible to every local user via ps")
        self.assertIn("PROMPT-TEXT", seen["stdin"])

    def test_errors_never_quote_the_judge_output(self):
        def echo(argv, **kw):
            return subprocess.CompletedProcess(argv, 0, "The user wrote: my bank PIN is 4417", "")

        verdict, error = judge.run_judge({"previous": "p", "prompt": "q"}, run=echo)
        self.assertEqual(verdict, "")
        self.assertNotIn("4417", error)

    def test_relative_judge_path_resolves_against_the_caller(self):
        self.assertEqual(judge.custom_argv("./ok --x", "/work")[0], "/work/ok")
        self.assertEqual(judge.custom_argv("ok", "/work")[0], "ok", "a bare name still goes through PATH")

    def test_announcement_does_not_replace_the_action_text(self):
        lessons = [{"tool": "Bash", "needle": "pop-to-buffer", "lesson": "x" * 25},
                   {"tool": "Bash", "needle": "moved my", "lesson": "y" * 25}]
        cs = [{"id": "a", "verdict": "correction", "prompt": "no, that moved my cursor"}]
        _, fired = reflex.build("Bash", "emacs pop-to-buffer", lessons, cs, set(), ["a"])
        self.assertIn("Bash:pop-to-buffer", fired)
        self.assertNotIn("Bash:moved my", fired)

    def test_key_after_an_escape_written_as_text_is_masked(self):
        # THE CLASS, not the instances: every escape a backslash can start (the bug was found one family at a time)
        escapes = ["\\" + chr(c) for c in range(33, 127) if chr(c) != "\\"]
        escapes += ["\\033", "\\012", "\\0", "\\101", "\\x1b", "\\u00e9", "\\U0001F600", "\\\\n"]
        for pre in escapes:
            self.assertNotIn(TOKEN, store.mask("X" + pre + TOKEN), pre)
            self.assertTrue(store.carries_secret("X" + pre + TOKEN), pre)
        self.assertFalse(store.carries_secret("xn" + TOKEN), "a key character before the prefix is not a boundary")
        aws = "AK" + "IA" + "Q3EGR7WZ5LXHJ2PN"  # first letters look like hex digits
        for w, width in (("x", 2), ("u", 4), ("U", 8)):
            for d in range(width):  # after the letter alone and after every partial hex run
                self.assertNotIn(aws, store.mask("X\\" + w + "F" * d + aws), (w, d))
        job = failure.job_for({"tool_name": "Bash", "session_id": "E", "error": "e",
                               "tool_input": {"command": "printf 'K\\012" + TOKEN + "' | curl -d @- x"}})
        self.assertTrue(job["action"])
        self.assertTrue(store.carries_secret(job["action"]), "the judge masks it; the floor must see it")

    def test_key_on_its_own_line_in_tool_arguments_is_masked(self):
        content = [{"type": "tool_use", "name": "Write", "input": {"file_path": "x", "content": "TOKENS\n" + TOKEN}}]
        self.assertNotIn(TOKEN, capture._reply(content))
        job = failure.job_for({"tool_name": "Write", "session_id": "M", "error": "e",
                               "tool_input": {"file_path": "x", "content": "TOKENS\n" + TOKEN}})
        self.assertNotIn(TOKEN, job["action"])

    def test_duplicate_found_despite_trailing_whitespace(self):
        judge.record({"session": "W", "prompt": "no, that's wrong", "previous": "p"}, "correction")
        dup = judge.record({"kind": "hindsight", "session": "W", "reply": "p", "prompt": "no, that's wrong\n",
                            "follow": "f"}, "error")
        self.assertEqual(dup["verdict"], "duplicate")

    def test_once_per_session(self):
        lessons = [{"tool": "Bash", "needle": "git push", "lesson": "x" * 25}]
        out, fired = reflex.build("Bash", "git push origin", lessons, [], set(), [])
        self.assertEqual(len(out), 1)
        self.assertEqual(reflex.build("Bash", "git push origin", lessons, [], fired, [])[0], [])
        self.assertEqual(reflex.build("Edit", "git push origin", lessons, [], set(), [])[0], [])

    def test_closed_correction_is_not_announced(self):
        cs = [{"id": "a", "verdict": "correction", "prompt": "no"}]
        self.assertEqual(reflex.build("Read", "", [], cs, set(), ["a"], {"a"})[0], [], "dismissed")
        self.assertEqual(reflex.build("Read", "", [{"source": "correction:a"}], cs, set(), ["a"])[0], [], "cited")
        self.assertEqual(len(reflex.build("Read", "", [], cs, set(), ["a"])[0]), 1)

    def test_mint_names_this_plugins_cli(self):
        out, _ = reflex.build("Bash", "ls", [], [{"id": "c1", "prompt": "no"}], set(), ["c1"])
        self.assertIn(CLI, out[0], "never a bare `lesson` resolved through PATH")
        self.assertNotIn("Bash|Edit", out[0], "nothing in the command a shell would read as a pipe")

    def test_state_keeps_recent_sessions(self):
        st = {}
        for i in range(80):
            st = store.merged(st, f"s{i}", {"k"}, keep=64)
        self.assertEqual(len(st), 64, "the cap is a parameter; production keeps store.KEEP_SESSIONS")
        self.assertIn("s79", st)
        self.assertNotIn("s0", st)

    def test_pending(self):
        cs = [{"id": "a", "verdict": "correction"}, {"id": "b", "verdict": "correction"},
              {"id": "c", "verdict": "request"}, {"id": "d", "verdict": "correction"}, {"verdict": "correction"}]
        got = store.pending(cs, [{"source": "correction:b"}], {"d"})
        self.assertEqual([c["id"] for c in got], ["a"])

    def test_session_files_do_not_collide(self):
        names = {store.session_file(s) for s in ("a/b", "ab", "", "none", "../../x", "x")}
        self.assertEqual(len(names), 6)

    def test_multiedit_text(self):
        self.assertEqual(reflex.action_text("MultiEdit", {"edits": [{"new_string": "a"}, {"new_string": "b"}]}), "a\nb")


class Transcript(unittest.TestCase):
    def setUp(self):
        self.d = tempfile.TemporaryDirectory()
        self.tp = os.path.join(self.d.name, "t.jsonl")

    def tearDown(self):
        self.d.cleanup()

    def test_reply_stops_at_the_previous_prompt(self):
        transcript(self.tp, [said("OLD TEXT from long ago"), typed("next question"),
                             {"type": "assistant", "message": {"content": [{"type": "tool_use", "name": "Bash",
                                                                            "input": {"command": "rm -r build"}}]}}])
        got = capture.last_reply(self.tp)
        self.assertNotIn("OLD TEXT", got)
        self.assertIn("rm -r build", got, "a tool-only turn is still what the user may be correcting")

    def test_image_and_slash_prompts_are_boundaries(self):
        image = {"type": "user", "message": {"content": [{"type": "image", "source": {}}]}}
        slash = typed("<command-message>review</command-message><command-name>/review</command-name>")
        for prompt_row in (image, slash):
            transcript(self.tp, [typed("q0"), said("OLD-REPLY"), prompt_row, said("NEW-REPLY")])
            self.assertEqual(capture.last_reply(self.tp), "NEW-REPLY")

    def test_task_notification_does_not_cut_the_reply(self):
        note_with_origin = {"type": "user", "origin": {"kind": "task-notification"},
                            "message": {"content": "<task-notification><task-id>t</task-id></task-notification>"}}
        note_bare = typed("<task-notification><task-id>t</task-id></task-notification>")
        for note in (note_with_origin, note_bare):
            transcript(self.tp, [typed("q0"), said("I deleted the cache"), note, said("Agent finished.")])
            got = capture.last_reply(self.tp)
            self.assertIn("I deleted the cache", got)
            self.assertIn("Agent finished.", got)

    def test_meta_rows_do_not_cut_the_reply(self):
        meta = {"type": "user", "isMeta": True, "message": {"content": "<system-reminder>x</system-reminder>"}}
        transcript(self.tp, [typed("q0"), said("FIRST"), meta, said("SECOND")])
        self.assertEqual(capture.last_reply(self.tp), "FIRST\nSECOND")

    def test_previous_and_follow(self):
        transcript(self.tp, [typed("q0"), said("It changed 3 lines."), typed("no -- it was 156"),
                             {"type": "assistant", "message": {"content": [{"type": "tool_use", "name": "Bash",
                                                                            "input": {"command": "git show --stat"}}]}},
                             {"type": "user", "message": {"content": [{"type": "tool_result",
                                                                       "content": "8 files changed, 156 lines"}]}},
                             said("You're right: 156 lines.")])
        reply, prompt, follow = capture.previous_and_follow(self.tp)
        self.assertEqual(reply, "It changed 3 lines.")
        self.assertEqual(prompt, "no -- it was 156")
        self.assertIn("git show --stat", follow)
        self.assertIn("156 lines]", follow, "this turn's tool RESULTS reach the judge")
        self.assertIn("You're right: 156 lines.", follow)

    def test_no_previous_turn_no_hindsight(self):
        transcript(self.tp, [typed("q0"), said("only one turn")])
        self.assertEqual(capture.previous_and_follow(self.tp), ("", "", ""))
        self.assertIsNone(hindsight.job_for({"transcript_path": self.tp, "session_id": "S"}))

    def test_harness_or_empty_prompt_is_not_a_follow_up(self):
        for p in ("<command-name>/clear</command-name>", "Stop hook feedback: keep going"):
            transcript(self.tp, [typed("q0"), said("a"), typed(p), said("b")])
            self.assertEqual(capture.previous_and_follow(self.tp), ("", "", ""), p)

    def test_hindsight_skips_stop_hook_active(self):
        transcript(self.tp, [typed("q0"), said("a"), typed("q1"), said("b")])
        self.assertIsNone(hindsight.job_for({"transcript_path": self.tp, "stop_hook_active": True}))
        self.assertEqual(hindsight.job_for({"transcript_path": self.tp, "session_id": "S"})["kind"], "hindsight")

    def test_same_reply_judged_once_across_a_task_notification(self):
        note = {"type": "user", "origin": {"kind": "task-notification"},
                "message": {"content": "<task-notification>x</task-notification>"}}
        transcript(self.tp, [typed("q0"), said("A0"), typed("q1"), said("A1")])
        self.assertIsNotNone(hindsight.job_for({"transcript_path": self.tp, "session_id": "N"}))
        transcript(self.tp, [typed("q0"), said("A0"), typed("q1"), said("A1"), note, said("A2")])
        self.assertIsNone(hindsight.job_for({"transcript_path": self.tp, "session_id": "N"}), "A0 already judged")

    def test_interrupted_turn_is_judged(self):
        transcript(self.tp, [typed("clean up"), said("Cleaning the cache directory."),
                             {"type": "assistant", "message": {"content": [{"type": "tool_use", "name": "Bash",
                                                                            "input": {"command": "rm -r ~/data"}}]}},
                             {"type": "user", "message": {"content": [{"type": "tool_result", "content": "interrupted"}]}},
                             {"type": "user", "message": {"content": [{"type": "text",
                                                                       "text": "[Request interrupted by user]"}]}}])
        job = capture.payload({"prompt": "NO, not that directory", "transcript_path": self.tp, "session_id": "S"})
        self.assertIsNotNone(job)
        self.assertIn("rm -r ~/data", job["previous"])
        self.assertIn("Cleaning the cache", job["previous"])

    def test_reply_survives_odd_lines(self):
        transcript(self.tp, [said("the answer"), "[1, 2]", "42", "{torn"])
        self.assertEqual(capture.last_reply(self.tp), "the answer")

    def test_tool_results_are_not_prompts(self):
        transcript(self.tp, [said("the answer"),
                             {"type": "user", "message": {"content": [{"type": "tool_result", "content": "ok"}]}}])
        self.assertEqual(capture.last_reply(self.tp), "the answer")

    def test_harness_turns_skipped_but_angle_bracket_prompts_judged(self):
        transcript(self.tp, [said("x")])
        ev = {"transcript_path": self.tp, "session_id": "S"}
        self.assertIsNone(capture.payload(dict(ev, prompt="<task-notification>x")))
        self.assertIsNone(capture.payload(dict(ev, prompt="Stop hook feedback: keep going")))
        self.assertIsNotNone(capture.payload(dict(ev, prompt="<- that line is wrong")))


class EndToEnd(unittest.TestCase):
    """capture -> judge (stubbed) -> inbox -> reflex, as the hooks run them."""

    def setUp(self):
        self.d = tempfile.TemporaryDirectory()
        self.home = os.path.join(self.d.name, "home")
        self.marker = os.path.join(self.d.name, "judge-ran")
        stub = os.path.join(self.d.name, "stub")
        with open(stub, "w") as fh:
            fh.write(f"#!/bin/sh\ncat >/dev/null\ntouch {self.marker}\necho correction\n")
        os.chmod(stub, 0o755)
        self.env = dict(os.environ, LESSON_REFLEX_HOME=self.home, LESSON_REFLEX_JUDGE=stub)
        self.env.pop("LESSON_REFLEX_JUDGING", None)
        self.tp = os.path.join(self.d.name, "t.jsonl")
        transcript(self.tp, [said("Done: it will not move your cursor.")])

    def tearDown(self):
        self.d.cleanup()

    def wait_for(self, p, timeout=10):
        end = time.time() + timeout
        while time.time() < end:
            if os.path.exists(p):
                return True
            time.sleep(0.05)
        return False

    def capture(self, prompt, session="S1", env=None):
        r = run(os.path.join(HOOKS, "capture.py"),
                {"prompt": prompt, "transcript_path": self.tp, "session_id": session}, env or self.env)
        self.assertEqual((r.returncode, r.stdout), (0, ""), "capture returns at once and prints nothing")

    def test_flow(self):
        self.capture("no, it moved my cursor")
        self.assertTrue(self.wait_for(store.inbox_path("S1").replace(store.home(), self.home)) or
                        self.wait_for(os.path.join(self.home, "inbox", store.session_file("S1"))))
        pre = {"tool_name": "Read", "tool_input": {"file_path": "x"}, "session_id": "S1"}
        out = run(os.path.join(HOOKS, "reflex.py"), pre, self.env).stdout
        self.assertIn("CORRECTED", out, "announced at any tool call, not only Bash/Edit/Write")
        self.assertEqual(run(os.path.join(HOOKS, "reflex.py"), pre, self.env).stdout, "", "announced once")
        self.assertEqual(run(os.path.join(HOOKS, "reflex.py"), dict(pre, session_id="S2"), self.env).stdout, "",
                         "another session is not told about S1's correction")
        rows = store.read_jsonl(os.path.join(self.home, "corrections.jsonl"))
        self.assertEqual(rows[0]["prompt"], "no, it moved my cursor")
        add = subprocess.run([sys.executable, CLI, "add", "--tool", "Bash", "--needle", " deploy-prod ",
                              "--lesson", "run the dry run first; the real deploy broke staging",
                              "--source", "correction:" + rows[0]["id"]], capture_output=True, text=True, env=self.env, check=False)
        self.assertEqual(add.returncode, 0, add.stderr)
        self.assertEqual(store.read_jsonl(os.path.join(self.home, "lessons.jsonl"))[0]["needle"], "deploy-prod")
        pend = subprocess.run([sys.executable, CLI, "pending"], capture_output=True, text=True, env=self.env, check=False).stdout
        self.assertIn("0 correction(s)", pend)
        fire = run(os.path.join(HOOKS, "reflex.py"),
                   {"tool_name": "Bash", "tool_input": {"command": "x deploy-prod y"}, "session_id": "S3"}, self.env)
        self.assertIn("run the dry run first", fire.stdout)
        mode = os.stat(os.path.join(self.home, "corrections.jsonl")).st_mode & 0o777
        self.assertEqual(mode, 0o600, "the ledger is private")

    def test_non_corrections_keep_no_text(self):
        stub = os.path.join(self.d.name, "req")
        with open(stub, "w") as fh:
            fh.write(f"#!/bin/sh\ncat >/dev/null\ntouch {self.marker}\necho request\n")
        os.chmod(stub, 0o755)
        self.capture("please also add tests", env=dict(self.env, LESSON_REFLEX_JUDGE=stub))
        self.assertTrue(self.wait_for(self.marker))
        path = os.path.join(self.home, "corrections.jsonl")
        self.assertTrue(self.wait_for(path))
        time.sleep(0.2)
        row = store.read_jsonl(path)[0]
        self.assertEqual(row["verdict"], "request")
        self.assertNotIn("prompt", row)

    def test_judge_failure_is_recorded_and_reported(self):
        self.capture("no", env=dict(self.env, LESSON_REFLEX_JUDGE="/nonexistent/judge"))
        path = os.path.join(self.home, "corrections.jsonl")
        self.assertTrue(self.wait_for(path))
        time.sleep(0.2)
        self.assertEqual(store.read_jsonl(path)[0]["verdict"], "judge-error")
        pend = subprocess.run([sys.executable, CLI, "pending"], capture_output=True, text=True, env=self.env, check=False).stdout
        self.assertIn("did not answer 1 of the last 1", pend)

    def test_concurrent_sessions_lose_nothing(self):
        lessons = os.path.join(self.home, "lessons.jsonl")
        store.append_jsonl(lessons, {"tool": "Bash", "needle": "make test", "lesson": "x" * 25})
        evs = [{"tool_name": "Bash", "tool_input": {"command": "make test"}, "session_id": f"P{i}"} for i in range(24)]
        with ThreadPoolExecutor(12) as ex:
            outs = list(ex.map(lambda ev: run(os.path.join(HOOKS, "reflex.py"), ev, self.env).stdout, evs))
        self.assertEqual(sum("LESSON" in o for o in outs), 24, "every session got its lesson")
        with open(os.path.join(self.home, "fired.json")) as fh:
            self.assertEqual(len(json.load(fh)), 24, "no session's fired set was lost")

    def test_inbox_entry_queued_mid_announcement_survives(self):
        os.environ["LESSON_REFLEX_HOME"] = self.home
        try:
            store.append_jsonl(os.path.join(self.home, "corrections.jsonl"), {"id": "c1", "verdict": "correction"})
            store.append_jsonl(os.path.join(self.home, "corrections.jsonl"), {"id": "c2", "verdict": "correction"})
            store.add_to_inbox("S9", "c1")
            # the judge queues c2 after reflex read [c1] but before it consumes: only c1 may be removed
            store.add_to_inbox("S9", "c2")
            reflex.consume_inbox("S9", {"c1"})
            self.assertEqual(reflex.read_inbox("S9"), ["c2"])
        finally:
            os.environ["LESSON_REFLEX_HOME"] = os.path.join(_HOME.name, "home")

    def test_subagent_does_not_take_the_announcement(self):
        self.capture("no, it moved my cursor")
        self.assertTrue(self.wait_for(os.path.join(self.home, "inbox", store.session_file("S1"))))
        sub = {"tool_name": "Read", "tool_input": {}, "session_id": "S1", "agent_id": "sub-1"}
        self.assertEqual(run(os.path.join(HOOKS, "reflex.py"), sub, self.env).stdout, "")
        main = dict(sub)
        del main["agent_id"]
        self.assertIn("CORRECTED", run(os.path.join(HOOKS, "reflex.py"), main, self.env).stdout)

    def test_garbled_judge_output_still_leaves_a_row(self):
        stub = os.path.join(self.d.name, "bad")
        with open(stub, "wb") as fh:
            fh.write(b"#!/bin/sh\ncat >/dev/null\nprintf '\\377\\376 garbage'\n")
        os.chmod(stub, 0o755)
        self.capture("no", env=dict(self.env, LESSON_REFLEX_JUDGE=stub))
        path = os.path.join(self.home, "corrections.jsonl")
        self.assertTrue(self.wait_for(path))
        time.sleep(0.2)
        self.assertEqual(store.read_jsonl(path)[0]["verdict"], "judge-error")

    def test_dismiss_requires_a_known_correction(self):
        r = subprocess.run([sys.executable, CLI, "dismiss", "nope"], capture_output=True, text=True, env=self.env, check=False)
        self.assertEqual(r.returncode, 64)
        store.append_jsonl(os.path.join(self.home, "corrections.jsonl"), {"id": "r1", "verdict": "request"})
        r = subprocess.run([sys.executable, CLI, "dismiss", "r1"], capture_output=True, text=True, env=self.env, check=False)
        self.assertEqual(r.returncode, 64, "only a correction can be dismissed")

    def test_rm_strips_the_needle_like_add(self):
        subprocess.run([sys.executable, CLI, "add", "--tool", "Bash", "--needle", " deploy-prod ", "--lesson",
                        "x" * 25], capture_output=True, env=self.env, check=False)
        r = subprocess.run([sys.executable, CLI, "rm", "Bash", " deploy-prod "], capture_output=True, text=True,
                           env=self.env, check=False)
        self.assertEqual(r.returncode, 0, r.stderr)

    def test_hindsight_flow(self):
        stub = os.path.join(self.d.name, "err")
        with open(stub, "w") as fh:
            fh.write(f"#!/bin/sh\ncat >/dev/null\ntouch {self.marker}\necho error\n")
        os.chmod(stub, 0o755)
        transcript(self.tp, [typed("q0"), said("Done: it will not move your cursor."), typed("it moved my cursor"),
                             said("You're right, it did.")])
        env = dict(self.env, LESSON_REFLEX_JUDGE=stub)
        r = run(os.path.join(HOOKS, "hindsight.py"), {"transcript_path": self.tp, "session_id": "H1"}, env)
        self.assertEqual((r.returncode, r.stdout), (0, ""), "a Stop hook that never blocks or prints")
        self.assertTrue(self.wait_for(os.path.join(self.home, "inbox", store.session_file("H1"))))
        out = run(os.path.join(HOOKS, "reflex.py"), {"tool_name": "Read", "tool_input": {}, "session_id": "H1"},
                  env).stdout
        self.assertIn("PREVIOUS reply was wrong", out)
        self.assertIn("not move your cursor", out)

    def test_failure_flow(self):
        stub = os.path.join(self.d.name, "mis")
        with open(stub, "w") as fh:
            fh.write(f"#!/bin/sh\ncat >/dev/null\ntouch {self.marker}\necho mistake\n")
        os.chmod(stub, 0o755)
        env = dict(self.env, LESSON_REFLEX_JUDGE=stub)
        ev = {"hook_event_name": "PostToolUseFailure", "tool_name": "Bash",
              "tool_input": {"command": "arc-cdp --help", "description": "Read usage"},
              "error": "Exit code 64 unknown command", "session_id": "F1"}
        r = run(os.path.join(HOOKS, "failure.py"), ev, env)
        self.assertEqual((r.returncode, r.stdout), (0, ""))
        self.assertTrue(self.wait_for(os.path.join(self.home, "inbox", store.session_file("F1"))))
        out = run(os.path.join(HOOKS, "reflex.py"), {"tool_name": "Read", "tool_input": {}, "session_id": "F1"},
                  env).stdout
        self.assertIn("YOUR mistake", out)
        self.assertIn("arc-cdp --help", out)

    def test_hindsight_off_and_judging_guard(self):
        transcript(self.tp, [typed("q0"), said("a"), typed("q1"), said("b")])
        for extra in ({"LESSON_REFLEX_HINDSIGHT": "0"}, {"LESSON_REFLEX_JUDGING": "1"}):
            r = run(os.path.join(HOOKS, "hindsight.py"), {"transcript_path": self.tp, "session_id": "OFF2"},
                    dict(self.env, **extra))
            self.assertEqual((r.returncode, r.stdout), (0, ""))
        time.sleep(0.5)
        self.assertFalse(os.path.exists(os.path.join(self.home, "hindsight-seen.jsonl")))

    def test_failures_can_be_turned_off(self):
        ev = {"tool_name": "Bash", "tool_input": {"command": "x --bad"}, "error": "e", "session_id": "OFF"}
        r = run(os.path.join(HOOKS, "failure.py"), ev, dict(self.env, LESSON_REFLEX_FAILURES="0"))
        self.assertEqual(r.returncode, 0)
        time.sleep(0.5)
        self.assertFalse(os.path.exists(os.path.join(self.home, "failures-seen.jsonl")), "nothing judged or recorded")

    def test_ast_state_is_private(self):
        ev = {"tool_name": "Write", "session_id": "P1",
              "tool_input": {"file_path": os.path.join(self.d.name, "t.py"), "content": "x = redact(s[:9])\n"}}
        run(os.path.join(HOOKS, "ast_reflex.py"), ev, self.env)
        mode = os.stat(os.path.join(self.home, "ast-fired.json")).st_mode & 0o777
        self.assertEqual(mode, 0o600)

    def test_ast_reflex_warns_before_the_edit(self):
        target = os.path.join(self.d.name, "tool.py")
        ev = {"tool_name": "Write", "session_id": "A1",
              "tool_input": {"file_path": target, "content": "def f(s):\n    return redact(s[:9])\n"}}
        out = run(os.path.join(HOOKS, "ast_reflex.py"), ev, self.env).stdout
        self.assertIn("slice-into-redact", out)
        self.assertFalse(os.path.exists(target), "a lesson, not a gate: nothing is written by the hook")

    def test_judge_does_not_judge_itself(self):
        self.capture("no", env=dict(self.env, LESSON_REFLEX_JUDGING="1"))
        time.sleep(1)
        self.assertFalse(os.path.exists(self.marker), "the stub judge never ran")

    def test_notification_turn_skipped(self):
        self.capture("<task-notification>x</task-notification>")
        time.sleep(1)
        self.assertFalse(os.path.exists(self.marker), "the stub judge never ran")


if __name__ == "__main__":
    unittest.main()
