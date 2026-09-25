# QuoteIQ Gemini Setup Guide

## Quick Start (5 minutes)

### Option 1: Use Gemini (Free, Recommended)

1. **Get a free API key:**
   - Go to https://ai.google.dev
   - Click **"Get Started for Free"**
   - Sign in with your Google account (create one if needed)
   - Click **"Get API key"** at https://ai.google.dev/apikey
   - Copy your key

2. **Configure the key:**
   ```powershell
   # In the project directory, edit .env (or create from .env.example)
   Copy-Item .env.example .env -Force  # if .env doesn't exist
   
   # Open .env in your editor and set:
   # AI_PROVIDER=gemini
   # GEMINI_API_KEY=<paste-your-key-here>
   # GEMINI_MODEL=gemini-3.5-flash-lite
   ```

3. **Verify it works:**
   ```powershell
   .\.venv\Scripts\python -c "from quoteiq import llm; print(llm.provider_name(), llm.model_name(), llm.configured())"
   ```
   
   This verifies local configuration only. A real API check happens when you generate an RFx or extract a quote.

4. **Run the app:**
   ```powershell
   .\.venv\Scripts\python -m streamlit run streamlit_app.py
   ```

### Option 2: Use OpenAI (Paid)

1. **Get an API key:**
   - Go to https://platform.openai.com/account/billing/overview
   - Add a payment method
   - Go to https://platform.openai.com/api-keys
   - Create a new API key

2. **Configure the key:**
   ```powershell
   # In .env set:
   # OPENAI_API_KEY=<your-key>
   # OPENAI_MODEL=gpt-4.1
   ```

3. **Run the app:**
   ```powershell
   .\.venv\Scripts\python -m streamlit run streamlit_app.py
   ```

## Testing Your Setup

### Test 1: Basic Extraction (Sandbox)
1. Open http://localhost:8501
2. Go to **Overview** → **Prepare demo workspace**
3. Go to **Quote inbox** → Try uploading one of the demo files (e.g., `demo_files/rfx.json`)
4. Click **Extract** 
5. Should see extraction progress and results with your chosen provider

### Test 2: RFx Generation
1. Go to **RFx Studio**
2. Enter a brief RFx need (e.g., "Corrugated boxes for ecommerce")
3. Click **Generate 30-item RFx with AI**
4. Should see a complete RFx with 30 items, printed questions, and delivery terms

### Test 3: Analyst Queries
1. After extracting all 5 supplier quotes (or using demo data)
2. Go to **Compare & decide**
3. Under **AI Analyst**, ask a question like "Who has the lowest total cost?"
4. Should see the analysis with evidence citations

## Troubleshooting

### "No AI provider configured"
- Make sure `.env` exists in the project root
- Set `AI_PROVIDER=gemini` and verify `GEMINI_API_KEY` is set
- Restart the Streamlit app

### Gemini "401 Unauthorized"
- Check your API key is correct at https://ai.google.dev/apikey
- Verify it's pasted exactly without spaces in `.env`

### Gemini "429 Quota Exceeded"
- Gemini free tier has rate limits; wait a few seconds between requests
- Check usage at https://ai.google.dev/account

### OpenAI "HTTP 429 credit_balance_exhausted"
- Your OpenAI account has no API credits
- Add a payment method in Account Settings
- Or switch to Gemini (free)

## Verifying Implementation

Provider contract tests and the deterministic application suite:
```powershell
.\.venv\Scripts\python -m pytest -q --tb=line
```

The exact count changes as coverage grows; rely on the command's actual result.

The skipped live tests require a real API key and `QUOTEIQ_RUN_LIVE=1`. They verify generation, all five extraction formats, and the evidence-grounded analyst.

## What Works

✅ RFx generation with AI  
✅ Multi-format quote extraction (PDF, XLSX, DOCX, JPG, EMAIL)  
✅ Human review and corrections  
✅ Comparison matrix with calculations  
✅ AI-powered analyst with read-only queries  
✅ Full exports (workbook + evidence)  
✅ Both OpenAI and Gemini providers  

## What's Demo/Offline

- Supplier invitations (simulated email, not sent)
- Demo RFx seed (explicitly marked as fictional)
- Offline comparison when no AI provider configured
