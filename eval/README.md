# Evaluation

Measures the running system against a labeled dataset, through its real API, with real OCR and the real LLM. The results are in [report.md](report.md).

| File | Purpose |
|---|---|
| `generate_dataset.py` | Builds the synthetic documents in `dataset/` and their right answers in `labels.json`. Deterministic: the same documents and labels every time, though PDFs and workbooks embed an ID or timestamp, so their bytes differ |
| `scoring.py`, `test_scoring.py` | Compares results with labels, and tests that comparison |
| `run_eval.py` | Uploads everything, waits, scores it, and writes `results/<name>.json` |
| `throughput.py` | Measures OCR throughput with one and two workers |
| `build_report.py` | Writes `report.md` from the results, so its numbers cannot be typed by hand |
| `notes.md` | The report's hand-written commentary, included as it is |
| `results/` | The measured results of each run, with the system's raw answers, and the throughput measurement |

## Run it

An evaluation uses its own empty database, because earlier documents would otherwise show up as false duplicates. It needs `LLM_API_KEY` in `.env`, and uses LLM quota.

```bash
pip install -r backend/requirements-dev.txt
python eval/generate_dataset.py

export BACKEND_HOST_PORT=8001 POSTGRES_HOST_PORT=5433 REDIS_HOST_PORT=6380
COMPOSE="docker compose -p docintel-eval -f docker-compose.yml -f eval/docker-compose.eval.yml"
$COMPOSE up -d --wait backend worker
sleep 60   # the workers load the embedding model

python eval/run_eval.py --api http://127.0.0.1:8001 --name final
python eval/throughput.py --api http://127.0.0.1:8001
python eval/build_report.py --runs final baseline

$COMPOSE down --volumes
```

To try another model, set `EVAL_LLM_MODEL` before `up`, start from an empty database, and pass a different `--name`. `build_report.py` reports the first run named in full and compares the others with it.

Use `127.0.0.1` rather than `localhost`: on Windows, `localhost` tries IPv6 first and adds about two seconds to every request.

The scorer's own tests: `pytest eval/test_scoring.py`.
