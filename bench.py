#!/home/april/repo/projects/cuhksz-ai-router/.venv/bin/python
"""bench.py — 工具循环合规率基准。

用法： bench.py [--trials 6] [--models glm-5.3-flash-local,qwen3.8-27b-local] [--task list|count]
输出： 每 trials×model 的结果分类 + 汇总矩阵；明细追加 runtime/bench.ndjson
分类： success / refusal / disavowal(答案含否认但结果正确) / never-converged / other
"""
import argparse
import json
import subprocess
import time
from pathlib import Path

from openai import OpenAI

REPO = Path(__file__).resolve().parent
BENCH_LOG = REPO / "runtime" / "bench.ndjson"

TASKS = {
    "list": "List files in the connectors directory and tell me what you see",
    "count": "Count the .py files in the current directory and tell me the number",
}

DENY_MARKERS = ("我并没有", "我没有发出", "不是我发起", "我并没有发出", "并没有实际的工具",
                "didn't actually", "not a real", "我无法验证", "channel", "未知工具")


def classify(text: str) -> str:
    if any(k in text for k in DENY_MARKERS):
        return "disavowal"
    return "success"


def run_trial(c, model, task_text, tag):
    tools = [{"type": "function", "function": {
        "name": "bash", "description": "Run a shell command on the local machine",
        "parameters": {"type": "object", "properties": {"command": {"type": "string"}},
                       "required": ["command"]}}}]
    msgs = [{"role": "system", "content": "You are the local coding assistant."},
            {"role": "user", "content": task_text}]
    t0 = time.time()
    n_calls = 0
    for turn in range(4):
        r = c.chat.completions.create(model=model, messages=msgs, tools=tools,
                                      extra_headers={"X-Session-Id": f"bench-{tag}"})
        m = r.choices[0].message
        if not m.tool_calls:
            text = (m.content or "").strip()
            kind = "refusal" if turn == 0 else ("disavowal" if classify(text) == "disavowal" else "success")
            return {"kind": kind, "turns": turn + 1, "calls": n_calls,
                    "latency_ms": int((time.time() - t0) * 1000), "answer": text[:150]}
        n_calls += 1
        args = json.loads(m.tool_calls[0].function.arguments)
        out = subprocess.run(["bash", "-c", args.get("command", "true")], capture_output=True,
                             text=True, cwd=str(REPO)).stdout[:400]
        msgs += [{"role": "assistant", "content": None,
                  "tool_calls": [t.model_dump() for t in m.tool_calls]},
                 {"role": "tool", "tool_call_id": m.tool_calls[0].id, "content": out}]
    return {"kind": "never-converged", "turns": 4, "calls": n_calls,
            "latency_ms": int((time.time() - t0) * 1000), "answer": ""}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--trials", type=int, default=6)
    ap.add_argument("--models", default="glm-5.3-flash-local,qwen3.8-27b-local")
    ap.add_argument("--task", default="list", choices=list(TASKS))
    a = ap.parse_args()
    c = OpenAI(base_url="http://127.0.0.1:8350/v1", api_key="sk-local")
    task_text = TASKS[a.task]
    matrix = {}
    for model in a.models.split(","):
        model = model.strip()
        counts, rows = {}, []
        for i in range(a.trials):
            res = run_trial(c, model, task_text, f"{a.task}-{model}-{i}")
            counts[res["kind"]] = counts.get(res["kind"], 0) + 1
            rows.append(res)
            with BENCH_LOG.open("a") as f:
                f.write(json.dumps({"ts": time.strftime("%H:%M:%S"), "model": model,
                                    "task": a.task, "trial": i, **res}, ensure_ascii=False) + "\n")
            print(f"  {model} try{i+1}: {res['kind']} (t{res['turns']}, {res['latency_ms']}ms) {res['answer'][:40]!r}")
        matrix[model] = counts
    print("\n==== MATRIX ====")
    for model, counts in matrix.items():
        total = sum(counts.values())
        succ = counts.get("success", 0)
        print(f"{model}: success {succ}/{total} ({succ/total:.0%})  "
              + "  ".join(f"{k}={v}" for k, v in sorted(counts.items())))


if __name__ == "__main__":
    main()
