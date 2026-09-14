"""The environment header in the log: version, session, GPU, HDR.

Users paste NeuralScreen.log into issues; the [env] header answers the
questions we would otherwise have to ask. On Windows those were the build
number, the driver version and whether HDR was on. Here the first question
is a different one and it is the most useful line in the log: which
compositor, and is there a layer shell - nearly everything in this port
behaves differently between wlroots, KDE and GNOME.

Every probe is wrapped - a missing library or a stripped system must not
crash the startup, the line is simply skipped.

Checked: the header prints the version, the session and the HDR state; a
broken probe (monkeypatched to raise) does not crash the caller.
"""
import importlib.util
import os
import sys

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BASE)
# main.py is a script with an entry point - import it under an explicit
# module name, otherwise `import main` binds the function main().
_spec = importlib.util.spec_from_file_location("ns_main", os.path.join(BASE, "main.py"))
ns_main = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(ns_main)


def main() -> int:
    failures = []

    # 1. The header prints the version and the OS.
    import io
    buf = io.StringIO()
    real_stdout = sys.stdout
    sys.stdout = buf
    try:
        ns_main._log_environment({"lang": "en", "profile": "Natural",
                                  "work_scale": 0.65})
    finally:
        sys.stdout = real_stdout
    text = buf.getvalue()
    print(text.strip())
    if f"NeuralScreen {ns_main.APP_VERSION}" not in text:
        failures.append("the version is missing from the header")
    if "session:" not in text:
        failures.append("the session line is missing from the header")
    if "HDR:" not in text:
        failures.append("the HDR line is missing")
    if "layer-shell:" not in text:
        failures.append("the layer-shell line is missing - it is the first "
                        "thing anyone reading a bug report needs")

    # 2. A broken probe must not crash the caller: the whole function is
    #    wrapped, so a raising probe leaves the rest of the header intact.
    import gpuinfo
    real_probe = gpuinfo.probe
    gpuinfo.probe = lambda *a, **k: (_ for _ in ()).throw(OSError("boom"))
    buf2 = io.StringIO()
    sys.stdout = buf2
    try:
        ns_main._log_environment({})
    finally:
        sys.stdout = real_stdout
        gpuinfo.probe = real_probe
    text2 = buf2.getvalue()
    if "NeuralScreen" not in text2:
        failures.append("a broken probe killed the whole header")

    print("=" * 60)
    if failures:
        print(f"FAIL: {len(failures)} - {failures}")
        return 1
    print("OK: the header carries version/session/layer-shell/HDR and "
          "survives a broken probe")
    return 0


if __name__ == "__main__":
    sys.exit(main())
