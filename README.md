# Mohan AllAI V2.2 (Fixed Image + GPT)

## Fixes
- Image: removed unsupported `response_format` and added url fallback.
- GPT: uses `client.responses.create(...)` because chat.completions can be unsupported.

## Run
```bash
pip install -r requirements.txt
cp .env.example .env
# edit .env
python allai.py
```
