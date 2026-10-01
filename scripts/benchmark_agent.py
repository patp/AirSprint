"""Measure offline cold CLI vs persistent JSONL overhead; never contact AirSprint."""
import json
from pathlib import Path
import statistics
import subprocess
import sys
import time


def main():
    executable = [sys.executable, str(Path(__file__).with_name("airsprint_cli.py"))]
    cold = []
    for _ in range(15):
        start = time.perf_counter()
        subprocess.run(executable + ["auth", "status"], check=True, capture_output=True)
        cold.append((time.perf_counter() - start) * 1000)
    process = subprocess.Popen(executable + ["agent", "serve"], stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    warm = []
    try:
        for index in range(101):
            start = time.perf_counter()
            process.stdin.write(json.dumps({"id": index, "command": "auth status", "arguments": {}}) + "\n")
            process.stdin.flush()
            result = json.loads(process.stdout.readline())
            if result["status"] != "ok":
                raise RuntimeError(result)
            if index:
                warm.append((time.perf_counter() - start) * 1000)
        process.stdin.close()
        process.wait(timeout=5)
    finally:
        if process.poll() is None:
            process.terminate()
            process.wait(timeout=5)
        process.stdout.close()
        process.stderr.close()
    skill = subprocess.run(executable + ["--skill"], capture_output=True, check=True).stdout
    form = subprocess.run(executable + ["agent", "describe", "--command", "trips list"], capture_output=True, check=True).stdout
    print(json.dumps({"operation": "auth status (offline)", "coldSamples": len(cold), "warmSamples": len(warm),
                      "coldMedianMs": round(statistics.median(cold), 3), "warmMedianMs": round(statistics.median(warm), 3),
                      "warmP95Ms": round(sorted(warm)[94], 3), "skillBytes": len(skill), "oneFormBytes": len(form)}, indent=2))


if __name__ == "__main__":
    main()
