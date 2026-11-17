"""Entry point.

  uv run adm            launch the GUI
  uv run adm --selftest parse/scan everything headless + edit round-trip on a copy
                        (+ fixtures for gemini/qwen/goose/cline/roo/opencode)
  uv run adm --repair   fix claude transcripts with dangling parentUuid/leafUuid
  uv run adm --smoke    launch GUI and auto-close after 4s
  uv run adm --transplant <id|file> --to claude|codex|opencode|gemini|qwen|goose|cline|roo|json
                        [--cwd DIR] [--title T] [--out DIR]
                        [--no-tools] [--reasoning] [--sidechain]
                        <id> matches any scanned source (claude/codex/opencode/
                        gemini/qwen/goose/cline/roo)
"""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path


def _posix(path) -> str:
    """Windows path -> POSIX path for the shell scripts used in tests."""
    cyg = shutil.which("cygpath")
    if cyg:
        try:
            return subprocess.check_output(
                [cyg, "-u", str(path)], text=True,
                encoding="utf-8", errors="replace").strip()
        except (OSError, subprocess.SubprocessError):
            pass
    return str(path).replace("\\", "/")


def repair_chains() -> int:
    """Repair claude transcripts whose parentUuid / last-prompt.leafUuid point
    at deleted records (sessions the CLI can no longer render). Every fixed
    file is backed up first; running sessions are skipped."""
    from adm import claude_store
    from adm import config as cfg
    from adm.jsonlio import read_lines

    running = claude_store.running_session_ids()
    fixed = skipped = clean = 0
    files = sorted(cfg.CLAUDE_PROJECTS.rglob("*.jsonl")) \
        if cfg.CLAUDE_PROJECTS.is_dir() else []
    for f in files:
        if f.stem in running:
            skipped += 1
            print(f"SKIP (running) {f}")
            continue
        try:
            pre = claude_store.dangling_counts(read_lines(f))
        except OSError:
            continue
        if pre == (0, 0):
            clean += 1
            continue
        n = claude_store.repair_file_chain(f)
        post = claude_store.dangling_counts(read_lines(f))
        print(f"FIXED {f.relative_to(cfg.CLAUDE_PROJECTS)}: "
              f"parent={pre[0]} leaf={pre[1]} -> {post} ({n} lines)")
        fixed += 1
    print(f"checked {len(files)} files: {fixed} repaired, {skipped} skipped "
          f"(running), {clean} clean")
    return 0


def selftest() -> int:
    from adm import claude_store, codex_store, local_store
    from adm.jsonlio import SessionEditor

    print("== scanning claude ==")
    cl = claude_store.scan_claude(lambda s: None)
    print(f"claude sessions: {len(cl)}")
    for m in cl[:3]:
        print(f"  [{m.id[:8]}] {m.display_title[:50]!r} proj={m.project} "
              f"msgs={m.n_messages} size={m.size} running={m.running}")

    print("== scanning codex ==")
    cx = codex_store.scan_codex(lambda s: None)
    print(f"codex sessions: {len(cx)}")
    for m in cx[:3]:
        print(f"  [{m.id[:8]}] {m.display_title[:50]!r} proj={m.project} "
              f"turns={m.n_messages} tokens={m.tokens} arch={m.archived}")

    ok = True
    for label, metas, loader in [
        ("claude", cl, claude_store.load_claude),
        ("codex", cx, codex_store.load_codex),
    ]:
        # pick a small session for a full parse check
        small = sorted(metas, key=lambda m: m.size)[:2]
        for m in small:
            sess = loader(m)
            print(f"== load {label} {m.id[:8]}: {sess.total_lines} lines, "
                  f"{len(sess.messages)} messages ==", flush=True)
            for msg in sess.messages[:3]:
                print(f"   {msg.role:10s} L{msg.line:<5d} "
                      f"{msg.plain_text(90)!r}")
            if not sess.messages and m.n_messages:
                print(f"   !! expected messages but parsed 0")
                ok = False

    # edit round-trip on a temp copy
    print("== edit round-trip ==")
    from adm import config as _cfg
    orig_backup = _cfg.BACKUP_DIR
    tmpdir = Path(tempfile.mkdtemp())
    _cfg.BACKUP_DIR = tmpdir / "backups"
    if cl:
        m = sorted(cl, key=lambda x: x.size)[0]
        tmp = tmpdir / "copy.jsonl"
        shutil.copy(m.path, tmp)
        ed = SessionEditor(tmp)
        ed.note_snapshot("claude", "test")
        claude_store.append_title(ed, "test", "selftest title")
        ed.save()
        ed2 = SessionEditor(tmp)
        ed2.note_snapshot("claude", "test")
        claude_store.append_user_message(ed2, "test", "C:\\tmp", "hello from selftest")
        ed2.save()
        txt = tmp.read_text(encoding="utf-8")
        assert "selftest title" in txt and "hello from selftest" in txt
        print("   claude backup + append + save + re-edit: OK")
    if cx:
        cands = [x for x in cx if x.n_messages > 0]
        m = sorted(cands, key=lambda x: x.size)[0]
        tmp = tmpdir / "roll.jsonl"
        shutil.copy(m.path, tmp)
        from adm.models import SessionMeta
        fake = SessionMeta(source="codex", id="test", path=str(tmp),
                           project="t")
        sess = codex_store.load_codex(fake)
        ed = SessionEditor(tmp)
        ed.note_snapshot("codex", "test")
        codex_store.append_user_message(ed, "hello from codex selftest")
        if sess.messages:
            msg = sess.messages[0]
            codex_store.apply_text_edits(ed, msg, {0: "EDITED TEXT"})
        ed.save()
        txt = tmp.read_text(encoding="utf-8")
        assert "hello from codex selftest" in txt
        assert "EDITED TEXT" in txt
        print(f"   codex load+edit+append: OK ({sess.total_lines} lines)")

    # delete + chain repair: a raw line delete must dangle refs, and both
    # repair paths must leave the parentUuid/leafUuid graph consistent
    print("== delete + chain repair ==")
    from adm.jsonlio import read_lines
    from adm.models import SessionMeta
    cands = [m for m in cl if m.n_messages >= 6 and not m.subagent]
    if cands:
        m = sorted(cands, key=lambda x: x.size)[0]
        victim_src = None
        tmpf = tmpdir / "chain.jsonl"
        shutil.copy(m.path, tmpf)
        fake = SessionMeta(source="claude", id="chain-test", path=str(tmpf),
                           project="t")
        sess = claude_store.load_claude(fake)
        mids = [x for x in sess.messages[1:-1]
                if len(claude_store.linked_lines(sess, x)) == 1]
        if mids:
            victim_src = mids[len(mids) // 2]

            # (1) standalone repair on an already-broken file
            ed = SessionEditor(tmpf)
            ed.note_snapshot("claude", "chain-test")
            linked = claude_store.linked_lines(sess, victim_src)
            ed.delete_lines(linked)
            ed.save()  # deliberately save the broken state
            pre = claude_store.dangling_counts(read_lines(tmpf))
            assert pre != (0, 0), "expected dangling refs after raw delete"
            n = claude_store.repair_file_chain(tmpf)
            post = claude_store.dangling_counts(read_lines(tmpf))
            assert post == (0, 0), f"repair_file_chain left {post}"
            print(f"   raw delete dangles {pre} -> repair_file_chain "
                  f"({n} lines) -> clean: OK")

            # (2) in-editor relink at delete time (the production path)
            shutil.copy(m.path, tmpf)
            sess = claude_store.load_claude(fake)
            ed = SessionEditor(tmpf)
            ed.note_snapshot("claude", "chain-test")
            linked = claude_store.linked_lines(sess, victim_src)
            ed.delete_lines(linked)
            claude_store.repair_after_delete(ed, linked)
            ed.save()
            post = claude_store.dangling_counts(read_lines(tmpf))
            assert post == (0, 0), f"repair_after_delete left {post}"
            print("   delete + repair_after_delete -> clean: OK")
        else:
            print("   (no unpaired middle message found; skipped)")
    else:
        print("   (no session with >=6 messages; skipped)")
    # consciousness upload: bundle -> install.py on a temp "other machine"
    print("== consciousness upload round-trip ==")
    import json
    import sqlite3
    import subprocess
    from adm import transfer
    up_root = tmpdir / "upload"
    other_home = tmpdir / "other-machine"
    other_home.mkdir()
    upload_cands = [x for x in cl if not x.subagent and x.n_messages > 0]
    if upload_cands:
        m = sorted(upload_cands, key=lambda x: x.size)[0]
        b = transfer.export_session(m, up_root)
        md = json.loads((b / "metadata.json").read_text(encoding="utf-8"))
        work = other_home / "work-claude"
        chome = other_home / "claude-home"
        rc = subprocess.call(
            [sys.executable, str(b / "install.py"), "--cwd", str(work),
             "--claude-home", str(chome)], stdout=subprocess.DEVNULL)
        assert rc == 0, "install.py failed for claude"
        installed = (chome / "projects" / transfer.claude_project_dir_name(str(work))
                     / f"{md['new_id']}.jsonl")
        assert installed.exists(), "claude transcript not installed"
        recs = [json.loads(x) for x in
                installed.read_text(encoding="utf-8").splitlines() if x.strip()]
        assert all(r.get("sessionId") in (None, md["new_id"]) for r in recs), \
            "sessionId not rewritten"
        cwds = [r.get("cwd") for r in recs if r.get("cwd")]
        assert cwds and all(c == str(work) for c in cwds), "cwd not rewritten"
        from adm.models import SessionMeta
        fake = SessionMeta(source="claude", id=md["new_id"], path=str(installed),
                           project="t")
        assert claude_store.load_claude(fake).messages, "installed session unreadable"
        print(f"   claude bundle {b.name}: install.py -> transcript parsed: OK")

        # platform scripts: PowerShell installer end-to-end, sh syntax check
        for name in ("install.bat", "install.ps1", "install.sh", "install.py"):
            assert (b / name).exists(), f"bundle missing {name}"
        ps = shutil.which("pwsh") or shutil.which("powershell")
        if ps:
            work_ps = other_home / "work-claude-ps1"
            work_ps.mkdir()
            # GetFullPath may expand 8.3 short names in TMP, so compare the
            # long form (which is also what Node's process.cwd() returns).
            work_ps = work_ps.resolve()
            chome_ps = other_home / "claude-home-ps1"
            rc = subprocess.call(
                [ps, "-NoProfile", "-ExecutionPolicy", "Bypass",
                 "-File", str(b / "install.ps1"), "-Cwd", str(work_ps),
                 "-ClaudeHome", str(chome_ps)],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            assert rc == 0, f"install.ps1 failed for claude (rc={rc})"
            inst_ps = (chome_ps / "projects"
                       / transfer.claude_project_dir_name(str(work_ps))
                       / f"{md['new_id']}.jsonl")
            assert inst_ps.exists(), "install.ps1 did not write the transcript"
            recs_ps = [json.loads(x) for x in
                       inst_ps.read_text(encoding="utf-8").splitlines() if x.strip()]
            assert all(r.get("sessionId") in (None, md["new_id"]) for r in recs_ps), \
                "install.ps1: sessionId not rewritten"
            cwds_ps = [r.get("cwd") for r in recs_ps if r.get("cwd")]
            assert cwds_ps and all(c == str(work_ps) for c in cwds_ps), \
                "install.ps1: cwd not rewritten"
            print(f"   claude bundle {b.name}: install.ps1 -> transcript parsed: OK")
        sh = shutil.which("sh")
        if sh:
            rc = subprocess.call([sh, "-n", str(b / "install.sh")],
                                 stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            assert rc == 0, "install.sh has a syntax error"
            print(f"   claude bundle {b.name}: install.sh syntax: OK")

    if cx:
        from adm import config as _cfg2
        codex_home = other_home / "codex-home"
        codex_home.mkdir()
        shutil.copy(_cfg2.CODEX_STATE_DB, codex_home / "state_5.sqlite")
        m = sorted(cx, key=lambda x: x.size)[0]
        b = transfer.export_session(m, up_root)
        md = json.loads((b / "metadata.json").read_text(encoding="utf-8"))
        work = other_home / "work-codex"
        rc = subprocess.call(
            [sys.executable, str(b / "install.py"), "--cwd", str(work),
             "--codex-home", str(codex_home)], stdout=subprocess.DEVNULL)
        assert rc == 0, "install.py failed for codex"
        rolls = list((codex_home / "sessions").rglob(f"rollout-*-{md['new_id']}.jsonl"))
        assert rolls, "codex rollout not installed"
        import sqlite3
        con = sqlite3.connect(codex_home / "state_5.sqlite")
        try:
            row = con.execute("SELECT rollout_path, cwd FROM threads WHERE id=?",
                              (md["new_id"],)).fetchone()
        finally:
            con.close()
        assert row and str(rolls[0]) == row[0] and row[1] == str(work), \
            "codex thread not registered"
        print(f"   codex bundle {b.name}: install.py -> rollout + sqlite row: OK")

    # cross-tool transplant: neutral builder + Claude/JSON writers (no real stores)
    print("== cross-tool transplant ==")
    from adm import transplant
    src = None
    src_msgs = None
    for cand in sorted([x for x in cl + cx if not x.subagent and 2 <= x.n_messages],
                       key=lambda x: x.size):
        if cand.size > 2_000_000:
            continue
        try:
            neutral = transplant.build_transcript(
                transplant.load_source(cand).messages)
        except Exception:  # noqa: BLE001
            continue
        if sum(1 for x in neutral if x.role == "user") and \
                sum(1 for x in neutral if x.role == "assistant"):
            src, src_msgs = cand, neutral
            break
    if src and src_msgs:
        prev_claude_home = os.environ.get("CLAUDE_CONFIG_DIR")
        tp_home = tmpdir / "transplant-claude-home"
        os.environ["CLAUDE_CONFIG_DIR"] = str(tp_home)
        try:
            work = tmpdir / "transplant-work"
            res = transplant.write_transcript(
                src_msgs, "transplant selftest", str(work), "claude",
                source=src.source, source_name=src.source_badge,
                model=src.model or "")
            tp_file = Path(res.path)
            assert tp_file.is_file(), "transplant claude file missing"
            assert tp_file.parent.parent.name == "projects", "project dir layout"
            fake_tp = SessionMeta(source="claude", id=res.session_id,
                                  path=str(tp_file), project="t", cwd=res.cwd)
            tp_loaded = claude_store.load_claude(fake_tp)
            assert tp_loaded.messages, "transplanted claude session unreadable"
            assert claude_store.dangling_counts(tp_file.read_text(
                encoding="utf-8").splitlines()) == (0, 0), "transplant chain broken"
            print(f"   {src.source} -> claude ({len(src_msgs)} msgs): parse + chain: OK")

            gen = transplant.write_transcript(
                src_msgs, "transplant selftest", str(work), "json",
                source=src.source, source_name=src.source_badge,
                out_dir=tmpdir / "transplant-json")
            gj = json.loads((Path(gen.path) / "transcript.json").read_text(
                encoding="utf-8"))
            assert gj["format"] == "ai-dialog-transcript" and gj["messages"], \
                "generic transcript invalid"
            assert (Path(gen.path) / "transcript.md").is_file(), "markdown missing"
            print(f"   {src.source} -> generic json+md: OK")

            # cross-tool conversion carried by the bundle: the same source
            # session pre-rendered as another tool's transcript
            conv_bundle = transfer.export_session(src, tmpdir / "convert-bundle")
            conv_md = json.loads((conv_bundle / "metadata.json").read_text(
                encoding="utf-8"))
            tp = conv_md.get("transplant")
            if tp:
                for name in ("convert.bat", "convert.ps1", "convert.sh",
                             "transplant/claude.jsonl",
                             "transplant/codex.jsonl", "transplant/opencode.json",
                             "transplant/gemini.json", "transplant/goose.json",
                             "transplant/cline/api_conversation_history.json",
                             "transplant/cline/ui_messages.json",
                             "transplant/neutral/transcript.json",
                             "restore.bat", "restore.ps1", "restore.sh",
                             "restore-claude.bat", "restore-claude.ps1",
                             "restore-claude.sh", "restore-codex.bat",
                             "restore-codex.ps1", "restore-codex.sh",
                             "restore-opencode.bat", "restore-opencode.ps1",
                             "restore-opencode.sh", "restore-gemini.bat",
                             "restore-gemini.ps1", "restore-gemini.sh",
                             "restore-qwen.bat", "restore-qwen.ps1",
                             "restore-qwen.sh", "restore-goose.bat",
                             "restore-goose.ps1", "restore-goose.sh",
                             "restore-cline.bat", "restore-cline.ps1",
                             "restore-cline.sh", "restore-roo.bat",
                             "restore-roo.ps1", "restore-roo.sh",
                             "restore-json.bat",
                             "restore-json.ps1", "restore-json.sh"):
                    assert (conv_bundle / name).exists(), f"bundle missing {name}"
                for target in ("claude", "codex", "opencode", "gemini",
                               "qwen", "goose", "cline", "roo", "json"):
                    txt = (conv_bundle / f"restore-{target}.bat").read_text(
                        encoding="utf-8")
                    assert "__TARGET__" not in txt and target in txt, \
                        f"restore-{target}.bat was not rendered"

                # One-click restore: convert + read the new id, without
                # launching anything.  This also exercises convert.ps1.
                ps = shutil.which("pwsh") or shutil.which("powershell")
                if ps:
                    restore_home = tmpdir / "claude-home-restore"
                    restore_work = (tmpdir / "restored claude").resolve()
                    env = dict(os.environ)
                    env["CLAUDE_CONFIG_DIR"] = str(restore_home)
                    rc = subprocess.call(
                        [ps, "-NoProfile", "-ExecutionPolicy", "Bypass",
                         "-File", str(conv_bundle / "restore-claude.ps1"),
                         str(restore_work), "-NoLaunch"],
                        env=env, stdout=subprocess.DEVNULL,
                        stderr=subprocess.DEVNULL)
                    assert rc == 0, f"restore-claude.ps1 failed (rc={rc})"
                    restored = list(restore_home.rglob(
                        f"{tp['claude']['id']}.jsonl"))
                    assert restored, "restore-claude.ps1 did not write a transcript"
                    rrecs = [json.loads(x) for x in restored[0].read_text(
                        encoding="utf-8").splitlines() if x.strip()]
                    assert all(r.get("cwd") in (None, str(restore_work))
                               for r in rrecs), "restore: cwd not rewritten"
                    print("   bundle: restore-claude.ps1 -> transcript: OK")
                conv_work = (tmpdir / "converted codex").resolve()
                conv_work.mkdir()
                conv_home = tmpdir / "codex-home-conv"
                conv_home.mkdir()
                shutil.copy(_cfg.CODEX_STATE_DB, conv_home / "state_5.sqlite")
                rc = subprocess.call(
                    [sys.executable, str(conv_bundle / "convert.py"), "codex",
                     "--cwd", str(conv_work), "--codex-home", str(conv_home)],
                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                assert rc == 0, "convert.py codex failed"
                conv_rolls = list(conv_home.rglob(
                    f"rollout-*-{tp['codex']['id']}.jsonl"))
                assert conv_rolls, "convert.py did not write a rollout"
                conv_text = conv_rolls[0].read_text(encoding="utf-8")
                assert "__ADM_TARGET_CWD__" not in conv_text, "placeholder left"
                conv_recs = [json.loads(x) for x in conv_text.splitlines()
                             if x.strip()]
                assert conv_recs[0]["payload"]["cwd"] == str(conv_work), \
                    "converted cwd not rewritten"
                conv_db = sqlite3.connect(conv_home / "state_5.sqlite")
                try:
                    conv_row = conv_db.execute(
                        "SELECT rollout_path, cwd FROM threads WHERE id=?",
                        (tp["codex"]["id"],)).fetchone()
                finally:
                    conv_db.close()
                assert conv_row and conv_row[1] == str(conv_work), \
                    "converted thread not registered"
                print(f"   {src.source} -> codex ({tp['n_messages']} msgs): "
                      f"convert.py into a new folder: OK")

                # the shell converter must work without Python at all (claude)
                sh = shutil.which("sh")
                if sh:
                    for name in ("convert.sh", "restore.sh",
                                 "restore-claude.sh", "restore-codex.sh",
                                 "restore-opencode.sh", "restore-json.sh"):
                        rc = subprocess.call(
                            [sh, "-n", str(conv_bundle / name)],
                            stdout=subprocess.DEVNULL,
                            stderr=subprocess.DEVNULL)
                        assert rc == 0, f"{name} has a syntax error"
                    conv_claude_home = tmpdir / "claude-home-conv"
                    sh_work = tmpdir / "converted claude"
                    sh_work.mkdir()
                    env = dict(os.environ)
                    env["CLAUDE_CONFIG_DIR"] = _posix(conv_claude_home)
                    posix_work = _posix(sh_work)
                    rc = subprocess.call(
                        [sh, _posix(conv_bundle) + "/convert.sh",
                         "claude", posix_work],
                        env=env, stdout=subprocess.DEVNULL,
                        stderr=subprocess.DEVNULL)
                    assert rc == 0, "convert.sh claude failed"
                    sh_files = list(conv_claude_home.rglob("*.jsonl"))
                    assert sh_files, "convert.sh did not write a transcript"
                    resolved = subprocess.check_output(
                        [sh, "-c", 'cd "$1" && pwd -P', "_", posix_work],
                        text=True, encoding="utf-8", errors="replace").strip()
                    sh_recs = [json.loads(x) for x in
                               sh_files[0].read_text(encoding="utf-8").splitlines()
                               if x.strip()]
                    assert all(r.get("sessionId") in (None, tp["claude"]["id"])
                               for r in sh_recs), "convert.sh: sessionId not set"
                    assert all(r.get("cwd") in (None, resolved) for r in sh_recs), \
                        "convert.sh: cwd not rewritten"
                    assert claude_store.dangling_counts(
                        sh_files[0].read_text(encoding="utf-8").splitlines()) \
                        == (0, 0), "convert.sh: claude chain broken"
                    print(f"   {src.source} -> claude: convert.sh (no Python): OK")
        finally:
            if prev_claude_home is None:
                os.environ.pop("CLAUDE_CONFIG_DIR", None)
            else:
                os.environ["CLAUDE_CONFIG_DIR"] = prev_claude_home

    # ---- new sources: fixtures on temp dirs, full scan/load/edit loops ----
    print("== new sources (fixtures) ==")
    import hashlib as _hashlib
    from adm import (cline_store as cline_st, config as cfg_any,
                     gemini_store as gem_st, goose_store as goose_st,
                     opencode_store as oc_st, sources as sources_mod)

    _saved_cfg = {}
    for _k in ("GEMINI_DIR", "QWEN_DIR", "CLINE_TASKS", "ROO_TASKS",
               "OPENCODE_DB", "goose_sessions_dir", "APP_DIR"):
        _saved_cfg[_k] = getattr(cfg_any, _k, None)
    try:
        cfg_any.APP_DIR = tmpdir / "app"  # keep fixture paths out of the real cache
        # ---------- gemini / qwen ----------
        for tool, home_attr in (("gemini", "GEMINI_DIR"),
                                ("qwen", "QWEN_DIR")):
            fcwd = f"D:\\fixture\\{tool}"
            fhash = _hashlib.sha256(fcwd.encode("utf-8")).hexdigest()
            gdir = tmpdir / f"{tool}-home"
            chats = gdir / "tmp" / fhash / "chats"
            chats.mkdir(parents=True)
            gdoc = {
                "sessionId": f"{tool}-fix-001", "projectHash": fhash,
                "startTime": "2026-01-01T00:00:00.000Z",
                "lastUpdated": "2026-01-02T00:00:00.000Z",
                "messages": [
                    {"id": "m1", "timestamp": "2026-01-01T00:00:01.000Z",
                     "type": "user", "content": f"hello {tool} fixture"},
                    {"id": "m2", "timestamp": "2026-01-01T00:00:02.000Z",
                     "type": "gemini", "content": f"{tool} reply",
                     "model": "test-model"},
                ]}
            (chats / f"session-2026-01-01-{tool}-fix-001.json").write_text(
                json.dumps(gdoc, indent=2), encoding="utf-8")
            setattr(cfg_any, home_attr, gdir)
            ctx = sources_mod.ScanContext(
                cwd_hints=sources_mod._hash_hints({fcwd}))
            gm = gem_st.scan(lambda s: None, ctx, tool=tool)
            assert len(gm) == 1, f"{tool} scan found {len(gm)}"
            assert gm[0].cwd == fcwd, f"{tool} cwd hint not applied: {gm[0].cwd!r}"
            gs = gem_st.load(gm[0])
            assert len(gs.messages) == 2, f"{tool} load: {len(gs.messages)}"
            ed = gem_st.make_editor(gs)
            gem_st.apply_text_edits(ed, gs.messages[0], {0: "EDITED SOURCE"})
            gem_st.append_user_message(ed, gm[0], "appended from selftest")
            ed.save()
            gs2 = gem_st.load(gm[0])
            flat = "\n".join(p.text for m in gs2.messages for p in m.parts)
            assert "EDITED SOURCE" in flat and "appended from selftest" in flat
            # tail-merge: an external append made BETWEEN editor creation
            # and save must survive (prefix guard + tail carried over)
            fixfile = chats / f"session-2026-01-01-{tool}-fix-001.json"
            ed = gem_st.make_editor(gs2)
            gem_st.append_user_message(ed, gm[0], "second local append")
            gdoc2 = json.loads(fixfile.read_text(encoding="utf-8"))
            gdoc2["messages"].append(
                {"id": "ext1", "timestamp": "2026-01-01T00:00:03.000Z",
                 "type": "gemini", "content": "external tail"})
            fixfile.write_text(json.dumps(gdoc2, indent=2), encoding="utf-8")
            ed.save()
            gs3 = gem_st.load(gm[0])
            flat3 = "\n".join(p.text for m in gs3.messages for p in m.parts)
            assert "external tail" in flat3 and "second local append" in flat3, \
                f"{tool} tail merge lost records"
            print(f"   {tool}: scan(cwd hint) + load + edit + append + tail-merge: OK")

        # ---------- goose ----------
        godir = tmpdir / "goose-home"
        godir.mkdir()
        (godir / "20260101_000001.json").write_text(json.dumps({
            "id": "20260101_000001", "description": "goose fixture",
            "working_dir": "D:\\fixture\\goose",
            "conversation": {"messages": [
                {"role": "user", "created": 1767225600,
                 "content": [{"type": "text", "text": "hello goose"}]},
                {"role": "assistant", "created": 1767225601,
                 "content": [
                     {"type": "text", "text": "goose reply"},
                     {"type": "toolRequest", "id": "t1", "tool": "shell",
                      "input": {"command": "ls"}}]},
            ]},
            "message_count": 2,
            "created_at": "2026-01-01T00:00:00.000Z",
            "updated_at": "2026-01-01T00:00:01.000Z",
        }, indent=2), encoding="utf-8")
        cfg_any.goose_sessions_dir = lambda: godir
        gm = goose_st.scan(lambda s: None)
        assert len(gm) == 1 and gm[0].cwd == "D:\\fixture\\goose"
        gs = goose_st.load(gm[0])
        assert len(gs.messages) == 2
        assert any(p.kind == "tool_call" for p in gs.messages[1].parts), \
            "goose toolRequest not mapped to a tool_call part"
        ed = goose_st.make_editor(gs)
        goose_st.apply_text_edits(ed, gs.messages[1], {0: "EDITED GOOSE"})
        ed.delete_records(goose_st.linked_lines(gs, gs.messages[0]))
        goose_st.append_user_message(ed, gm[0], "goose appended")
        ed.save()
        gs2 = goose_st.load(gm[0])
        roles = [m.role for m in gs2.messages]
        assert roles == ["assistant", "user"], f"goose delete+append: {roles}"
        assert gs2.messages[0].parts[0].text == "EDITED GOOSE"
        print("   goose: scan + load + edit + delete + append: OK")

        # ---------- cline (roo shares the module) ----------
        tasks_root = tmpdir / "vscode-tasks"
        task_dir = tasks_root / "1758000000000"
        task_dir.mkdir(parents=True)
        hist = [
            {"role": "user", "ts": 1758000000000, "content": [
                {"type": "text",
                 "text": "Working directory: D:\\fixture\\cline\n# environment"}]},
            {"role": "assistant", "ts": 1758000001000, "content": [
                {"type": "text", "text": "cline reply"},
                {"type": "tool_use", "id": "tu1", "name": "Read",
                 "input": {"file_path": "x.txt"}}]},
            {"role": "user", "ts": 1758000002000, "content": [
                {"type": "tool_result", "tool_use_id": "tu1",
                 "content": "file contents"}]},
            {"role": "user", "ts": 1758000003000, "content": [
                {"type": "text", "text": "please fix it"}]},
        ]
        (task_dir / "api_conversation_history.json").write_text(
            json.dumps(hist, indent=2), encoding="utf-8")
        (task_dir / "ui_messages.json").write_text("[]", encoding="utf-8")
        (task_dir / "task_metadata.json").write_text("{}", encoding="utf-8")
        cfg_any.CLINE_TASKS = tasks_root
        gm = cline_st.scan(lambda s: None, tool="cline")
        assert len(gm) == 1 and gm[0].cwd == "D:\\fixture\\cline", \
            f"cline cwd: {gm[0].cwd!r}"
        assert gm[0].n_messages == 4
        gs = cline_st.load(gm[0])
        assert len(gs.messages) == 4
        # tool_use <-> tool_result pairing
        linked = cline_st.linked_lines(gs, gs.messages[1])
        assert linked == {1, 2}, f"cline linked_lines: {linked}"
        ed = cline_st.make_editor(gs)
        cline_st.apply_text_edits(ed, gs.messages[1], {0: "EDITED CLINE"})
        ed.save()
        gs2 = cline_st.load(gm[0])
        assert gs2.messages[1].parts[0].text == "EDITED CLINE", \
            "cline edit did not round-trip"
        ed2 = cline_st.make_editor(gs2)
        ed2.delete_records(cline_st.linked_lines(gs2, gs2.messages[1]))
        cline_st.append_user_message(ed2, gm[0], "cline appended")
        ed2.save()
        gs3 = cline_st.load(gm[0])
        assert len(gs3.messages) == 3, f"cline delete left {len(gs3.messages)}"
        assert gs3.messages[-1].parts[0].text == "cline appended"
        print("   cline: scan(cwd parse) + load + tool pairing + edit + delete: OK")

        # ---------- opencode (read-only sqlite) ----------
        oc_dir = tmpdir / "opencode-home"
        oc_dir.mkdir()
        odb = oc_dir / "opencode.db"
        con = sqlite3.connect(odb)
        con.executescript("""
        CREATE TABLE session_v2 (id TEXT, project_id TEXT, workspace_id TEXT,
            parent_id TEXT, fork_session_id TEXT, directory TEXT, path TEXT,
            title TEXT, slug TEXT, version TEXT, cost REAL,
            tokens_input INTEGER, tokens_output INTEGER, agent TEXT,
            model TEXT, time_created INTEGER, time_updated INTEGER,
            time_idle INTEGER, time_viewed INTEGER, time_archived INTEGER);
        CREATE TABLE session_message (id TEXT, session_id TEXT, type TEXT,
            seq INTEGER, time_created INTEGER, time_updated INTEGER, data TEXT);
        CREATE TABLE event_sequence (aggregate_id TEXT, seq INTEGER,
            owner_id TEXT);
        """)
        con.execute("INSERT INTO session_v2 VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    ("ses_fix001", "proj1", "ws1", None, None,
                     "D:/fixture/opencode", None, "opencode fixture", None,
                     "2.0", 0.0, 100, 50, "build",
                     '{"id":"test-model","providerID":"x"}',
                     1758000000000, 1758000005000, 1758000005000,
                     1758000005000, None))
        con.execute("INSERT INTO session_message VALUES (?,?,?,?,?,?,?)",
                    ("msg1", "ses_fix001", "user", 1, 1758000001000, None,
                     json.dumps({"time": {"created": 1758000001000},
                                 "text": "hello opencode",
                                 "files": [], "agents": []})))
        con.execute("INSERT INTO session_message VALUES (?,?,?,?,?,?,?)",
                    ("msg2", "ses_fix001", "assistant", 2, 1758000002000, None,
                     json.dumps({
                         "time": {"created": 1758000002000},
                         "agent": "build", "model": {"id": "test-model"},
                         "content": [
                             {"type": "reasoning", "text": "thinking…"},
                             {"type": "text", "text": "opencode reply"},
                             {"type": "tool", "id": "call_1", "name": "shell",
                              "state": {"status": "completed",
                                        "input": {"command": "ls"},
                                        "content": [{"type": "text",
                                                     "text": "file-a\nfile-b"}]}}],
                         "finish": "stop"})))
        con.commit()
        con.close()
        cfg_any.OPENCODE_DB = odb
        gm = oc_st.scan(lambda s: None)
        assert len(gm) == 1, f"opencode scan: {len(gm)}"
        m0 = gm[0]
        assert not m0.readonly and m0.cwd == "D:/fixture/opencode" \
            and m0.model == "test-model" and m0.n_messages == 2, \
            f"opencode meta: ro={m0.readonly} cwd={m0.cwd!r}"
        gs = oc_st.load(m0)
        assert [mm.line for mm in gs.messages] == [1, 2], "seq not kept as line"
        asst = gs.messages[1]
        kinds = [p.kind for p in asst.parts]
        assert kinds == ["thinking", "text", "tool_call", "tool_result"], \
            f"opencode parts: {kinds}"
        # edit: assistant text part (part idx 1) + tool input (idx 2)
        ed = oc_st.make_editor(gs)
        oc_st.apply_text_edits(ed, asst, {1: "EDITED OPENCODE",
                                          2: '{"command": "dir"}'})
        oc_st.append_user_message(ed, m0, "appended from selftest")
        ed.delete_records(oc_st.linked_lines(gs, gs.messages[0]))
        ed.save()
        gs2 = oc_st.load(m0)
        assert len(gs2.messages) == 2, f"opencode delete left {len(gs2.messages)}"
        assert gs2.messages[0].parts[1].text == "EDITED OPENCODE"
        assert gs2.messages[0].parts[2].text.strip() == "{\n  \"command\": \"dir\"\n}", \
            "tool input not rewritten"
        assert gs2.messages[1].parts[0].text == "appended from selftest"
        new_seq = max(mm.line for mm in gs2.messages)
        # event_sequence counter must be at/above the appended seq
        con = sqlite3.connect(odb)
        try:
            ev = con.execute("SELECT seq FROM event_sequence"
                             " WHERE aggregate_id='ses_fix001'").fetchone()
        finally:
            con.close()
        assert ev and ev[0] >= new_seq, f"seq allocator behind: {ev} < {new_seq}"
        # external-change guard: touching a snapshot row out from under the
        # editor must refuse the save (snapshot BEFORE the external write)
        ed = oc_st.make_editor(gs2)
        con = sqlite3.connect(odb)
        con.execute("UPDATE session_message SET data='{}' WHERE seq=2")
        con.commit()
        con.close()
        oc_st.append_user_message(ed, m0, "must not land")
        try:
            ed.save()
            raise SystemExit("opencode editor saved over an external change")
        except Exception as e:  # noqa: BLE001
            from adm.jsonlio import FileChangedError as _FCE
            assert isinstance(e, _FCE), f"expected FileChangedError, got {e!r}"
        # rename / duplicate / remove on the sqlite store
        oc_st.rename_session("ses_fix001", "renamed fixture")
        dup_id = oc_st.duplicate_session(m0)
        assert dup_id, "duplicate_session failed"
        gm3 = oc_st.scan(lambda s: None)
        by_id = {m.id: m for m in gm3}
        assert by_id["ses_fix001"].title == "renamed fixture"
        assert by_id[dup_id].n_messages == 2, "duplicate lost messages"
        dup_meta = by_id[dup_id]
        assert oc_st.remove_session(dup_meta), "remove_session failed"
        assert len(oc_st.scan(lambda s: None)) == 1
        assert sources_mod.editable("opencode"), "opencode must be editable"
        print("   opencode: scan + load + edit + append + delete + guard"
              " + rename + duplicate + remove: OK")

        # ---------- registry wiring ----------
        assert sources_mod.load(m0).total_lines == 2, "sources.load dispatch"
        assert sources_mod.resume_command(m0) == "opencode --session ses_fix001"
        assert sources_mod.resume_command(
            SessionMeta(source="cline", id="t", path="p", project="p")) is None
        print("   sources registry: load/resume dispatch: OK")

        # ---------- meta cache ----------
        from adm import meta_cache
        gm_a = goose_st.scan(lambda s: None)
        assert gm_a and gm_a[0].display_title == "goose fixture"
        gm_b = goose_st.scan(lambda s: None)  # warm: served from cache
        assert [x.id for x in gm_b] == [x.id for x in gm_a]
        assert gm_b[0].n_messages == gm_a[0].n_messages
        # a file change must invalidate its entry (title comes from the doc)
        godir.joinpath("20260101_000001.json").write_text(json.dumps({
            "id": "20260101_000001", "description": "goose fixture v2",
            "working_dir": "D:\\fixture\\goose",
            "conversation": {"messages": [
                {"role": "user", "created": 1767225600,
                 "content": [{"type": "text", "text": "hello goose"}]}]},
        }, indent=2), encoding="utf-8")
        gm_c = goose_st.scan(lambda s: None)
        assert gm_c[0].display_title == "goose fixture v2", \
            f"cache not invalidated: {gm_c[0].display_title!r}"
        meta_cache.save()
        cache_doc = json.loads((tmpdir / "app" / "scan_cache.json")
                               .read_text(encoding="utf-8"))
        assert cache_doc["entries"], "cache file empty"
        print("   meta cache: warm reuse + invalidation + persist: OK")
    finally:
        for _k, _v in _saved_cfg.items():
            if _v is not None:
                setattr(cfg_any, _k, _v)

    _cfg.BACKUP_DIR = orig_backup
    print("SELFTEST", "PASS" if ok else "FAIL")
    return 0 if ok else 1


def transplant_cli(argv: list[str]) -> int:
    """Headless cross-tool transplant, e.g.

    uv run adm --transplant 4f2a1c --to codex --cwd D:/hello
    uv run adm --transplant session.jsonl --to opencode --cwd D:/hello
    uv run adm --transplant opencode-export.json --to claude --cwd D:/hello

    SOURCE is a session id/prefix, a Claude/Codex .jsonl transcript, an
    OpenCode export .json, or a portable transcript.json.
    """
    import argparse
    import json
    import os
    from pathlib import Path

    from adm import transplant

    ap = argparse.ArgumentParser(prog="adm --transplant")
    ap.add_argument("source", help="session id / .jsonl / opencode-export.json")
    ap.add_argument("--to", required=True, choices=list(transplant.TARGETS))
    ap.add_argument("--cwd", default="", help="target working directory")
    ap.add_argument("--title", default="")
    ap.add_argument("--out", default="", help="output root for --to json")
    ap.add_argument("--no-tools", action="store_true",
                    help="drop tool call/result text")
    ap.add_argument("--reasoning", action="store_true",
                    help="include thinking/reasoning text")
    ap.add_argument("--sidechain", action="store_true",
                    help="include Claude subagent records")
    ns = ap.parse_args(argv)

    opt = transplant.TransplantOptions(
        include_tools=not ns.no_tools,
        include_reasoning=ns.reasoning,
        include_sidechain=ns.sidechain,
    )
    src = Path(ns.source).expanduser()
    try:
        if src.is_file() and src.suffix.lower() == ".json":
            raw = src.read_bytes()
            # PowerShell 5.1 `>` / Out-File writes UTF-16; accept both.
            text = raw.decode("utf-16") if raw[:2] in (b"\xff\xfe", b"\xfe\xff") \
                else raw.decode("utf-8-sig")
            data = json.loads(text)
            if data.get("format") == "ai-dialog-transcript":
                title, src_cwd, msgs = transplant.transcript_from_generic(data)
                source, source_name = "json", "portable transcript"
            else:
                title, src_cwd, msgs = transplant.transcript_from_opencode_export(
                    data, opt)
                source, source_name = "opencode", "OpenCode"
            res = transplant.write_transcript(
                msgs, ns.title or title, ns.cwd or src_cwd or os.getcwd(),
                ns.to, source=source, source_name=source_name,
                out_dir=ns.out or None, options=opt)
        else:
            meta = transplant.find_session(ns.source) if not src.is_file() \
                else transplant._meta_from_file(src)
            res = transplant.transplant(
                meta, ns.to, ns.cwd or meta.cwd or None,
                title=ns.title or None, options=opt,
                out_dir=ns.out or None)
    except transplant.TransplantError as e:
        print(f"transplant failed: {e}", file=sys.stderr)
        return 1
    except OSError as e:
        print(f"transplant failed: {e}", file=sys.stderr)
        return 1

    print(f"OK  {res.target}  {res.n_messages} messages")
    print(f"  id:       {res.session_id}")
    print(f"  title:    {res.title}")
    print(f"  cwd:      {res.cwd}")
    print(f"  path:     {res.path}")
    for w in res.warnings:
        print(f"  warning:  {w}")
    print(f"  resume:   {res.resume_hint()}")
    return 0


def main() -> int:
    args = sys.argv[1:]
    if "--selftest" in args:
        return selftest()
    if "--repair" in args:
        return repair_chains()
    if "--transplant" in args:
        return transplant_cli([a for a in args if a != "--transplant"])
    try:
        from PyQt6.QtCore import QSettings, QTimer
        from PyQt6.QtWidgets import QApplication
    except ImportError as e:
        print(f"PyQt6 missing: {e}\nrun: uv sync", file=sys.stderr)
        return 2
    from PyQt6.QtGui import QIcon
    from adm import config, i18n, util
    from adm.ui import theme
    from adm.ui.main_window import MainWindow

    config.ensure_app_dirs()
    i18n.set_lang(str(QSettings("ai-dialog-manager", "adm").value("lang", "zh")))
    app = QApplication(sys.argv)
    app.setStyleSheet(theme.QSS)
    app.setApplicationName("AI Dialog Manager")
    _ico = util.asset_path("app.ico")
    if _ico.exists():
        app.setWindowIcon(QIcon(str(_ico)))
    win = MainWindow()
    win.show()
    if "--smoke" in args and os.environ.get("ADM_AUTOTEST"):
        _run_autotest(app, win)
    elif "--smoke" in args:
        QTimer.singleShot(4000, app.quit)
    return app.exec()


def _run_autotest(app, win) -> None:
    """Drive the real UI: wait for scan -> load a session -> render chat ->
    build an edit dialog -> jump. Prints AUTOTEST PASS/FAIL and quits."""
    from PyQt6.QtCore import QTimer
    state = {"ok": True, "stage": "scan"}

    def fail(msg):
        state["ok"] = False
        print(f"AUTOTEST FAIL at {state['stage']}: {msg}", flush=True)
        app.quit()

    def wait_scan():
        if win.scan_thread and win.scan_thread.isRunning():
            QTimer.singleShot(1500, wait_scan)
            return
        if not win.metas:
            fail("no sessions scanned")
            return
        print(f"scan ok: {len(win.metas)} sessions", flush=True)
        # pick a mid-size claude + codex session
        cand = sorted([m for m in win.metas if not m.subagent and m.size > 5000],
                      key=lambda m: m.size)
        picks = [cand[0]] + [m for m in cand if m.source == "codex"][:1]
        state["queue"] = picks
        state["stage"] = "load"
        load_next()

    def load_next():
        if not state.get("queue"):
            print("AUTOTEST PASS", flush=True)
            app.quit()
            return
        m = state["queue"].pop(0)
        state["stage"] = f"load {m.source} {m.id[:8]}"
        win._load_selected(meta=m)
        QTimer.singleShot(2500, lambda: check_loaded(m))

    def check_loaded(m):
        if not win.loaded:
            # big sessions may still be parsing — give it more time
            if win.load_thread and win.load_thread.isRunning():
                QTimer.singleShot(3000, lambda: check_loaded(m))
                return
            fail(f"session {m.id[:8]} failed to load")
            return
        sess = win.loaded
        n = len(sess.messages)
        print(f"loaded {m.source} {m.id[:8]}: {sess.total_lines} lines, {n} msgs",
              flush=True)
        if m.source == "claude":
            try:
                _synthetic_ui_check(win)
            except AssertionError as e:
                fail(f"synthetic ui: {e}")
                return
            except Exception as e:  # noqa: BLE001
                fail(f"synthetic ui: {e}")
                return
        state["stage"] = "render"
        texts = [x for x in sess.messages if x.text_parts]
        if texts:
            try:
                from adm.ui.dialogs import MessageEditDialog
                dlg = MessageEditDialog(texts[0], win)
                dlg.new_texts()
                dlg.deleteLater()
            except Exception as e:  # noqa: BLE001
                fail(f"edit dialog: {e}")
                return
            win.chat.queue_jump(texts[0].line)
        load_next()

    QTimer.singleShot(1000, wait_scan)


def _synthetic_ui_check(win) -> None:
    """Drive the chat view on a synthetic 400-message session (no disk writes):
    pagination, jump auto-expansion and the batch-selection toolbar."""
    from adm.models import LoadedSession, Message, Part, SessionMeta
    from adm.ui.chat_view import PAGE

    meta = SessionMeta(source="claude", id="synthetic", path="synthetic.jsonl",
                       project="t")
    msgs = [Message(line=i, role="user" if i % 2 else "assistant", ts=None,
                    parts=[Part(kind="text", text=f"msg {i}")])
            for i in range(400)]
    synth = LoadedSession(meta=meta, messages=msgs, raw_lines=[],
                          total_lines=400)
    cv = win.chat
    real = cv.session
    # the user's persisted "newest first" setting would change pagination —
    # this check assumes forward order (it tests reverse explicitly below)
    cv.set_reverse(False)
    cv.set_session(synth)
    assert cv._rendered == PAGE, f"expected first page {PAGE}, got {cv._rendered}"
    # a jump to a deep message must auto-expand pages instead of forcing the
    # user to click "load more" repeatedly
    cv.queue_jump(350)
    assert 350 in cv._frames, "jump did not auto-expand pagination"
    assert cv._rendered >= 351, f"pages not expanded: {cv._rendered}"
    # batch selection toolbar
    cv.batch_btn.setChecked(True)
    assert cv.batch_bar.isVisible() and cv._frames[0].check is not None, \
        "batch mode did not render checkboxes"
    cv._select_visible()
    assert len(cv._selected_msgs()) == 400, "select-visible count wrong"
    from PyQt6.QtWidgets import QApplication
    cv._copy_selection()
    clipped = QApplication.clipboard().text()
    assert "msg 0" in clipped and "msg 399" in clipped, "copy missed messages"
    cv._clear_selection()
    assert not cv._selected_msgs(), "clear selection failed"
    cv.batch_btn.setChecked(False)
    assert not cv.batch_bar.isVisible() and cv._frames[0].check is None, \
        "leaving batch mode kept checkboxes"
    # reverse ("newest first") reading order + jump while reversed
    cv.set_reverse(True)
    assert cv._visible[0].line == 399 and cv._rendered == PAGE, \
        "reverse order did not start with the newest message"
    cv.queue_jump(10)
    assert 10 in cv._frames, "jump failed in reverse order"
    cv.set_reverse(False)
    assert cv._visible[0].line == 0, "forward order not restored"
    # anchor-index reload path used after deletions (read-only reload)
    cv.set_session(real)
    assert cv._rendered <= PAGE
    print("synthetic ui: pagination + jump auto-expand + batch select + "
          "reverse order OK", flush=True)


if __name__ == "__main__":
    raise SystemExit(main())
