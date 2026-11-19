# DukaanDost – Week 1: WhatsApp talks to your laptop

**Goal for this week:** you send "hi" from your phone → your laptop receives it → your laptop replies → you see the reply on WhatsApp.

---

## What each file does (read this first)

| File | Job | Who writes it |
|---|---|---|
| `app/config.py` | Reads your secret tokens from `.env` | AI (routine) |
| `app/db.py` | Saves every message in `dukaandost.db`; skips duplicates | AI (routine) |
| `app/whatsapp.py` | Sends a message through Meta's Send API | AI (routine) |
| `app/main.py` | The server: `GET /webhook` (Meta's check) and `POST /webhook` (new messages) | AI (routine) |
| `app/ledger.py` | Credit, payment, balance, undo | **YOU, in Week 2** |

The flow:

```
Your phone ──► Meta ──► ngrok ──► main.py (POST /webhook)
                                     │
                                     ├─ db.save_incoming()   (skip if duplicate)
                                     ├─ decide the reply
                                     └─ whatsapp.send_text() ──► Meta ──► Your phone
```

---

## Setup, step by step

### 1. Install Python packages
Open a terminal inside the `dukaandost` folder:
```
python -m venv venv
venv\Scripts\activate          (Windows)
source venv/bin/activate       (Mac/Linux)
pip install -r requirements.txt
```

### 2. Meta developer account
1. Go to developers.facebook.com and log in.
2. **My Apps → Create App** → choose **Business** type.
3. In the app, add the **WhatsApp** product.
4. Go to **WhatsApp → API Setup**. You will see:
   - a **test phone number** (free)
   - a **Temporary access token**
   - the **Phone number ID**
5. Under "To", **add your own phone number** and verify it with the OTP.
   (The test number can only message up to 5 verified numbers.)

### 3. Create your .env file
Copy `.env.example` to a new file named `.env` and paste in your token and Phone number ID.
Invent any secret word for `VERIFY_TOKEN`.

### 4. Start your server
```
uvicorn app.main:app --reload --port 8000
```
You should see `Uvicorn running on http://127.0.0.1:8000`.

### 5. Make it reachable with ngrok
1. Sign up free at ngrok.com, install it, and add your authtoken (their site shows the command).
2. In a **second** terminal:
```
ngrok http 8000
```
3. Copy the `https://....ngrok-free.app` address it shows.

### 6. Connect Meta to your server
1. Meta dashboard → **WhatsApp → Configuration → Webhook → Edit**.
2. Callback URL: `https://YOUR-NGROK-ADDRESS/webhook`
3. Verify token: the same word as `VERIFY_TOKEN` in your `.env`.
4. Click **Verify and save**. Your server terminal should print `[webhook] verified by Meta`.
5. Under **Webhook fields**, **Subscribe** to `messages`.

### 7. Test it 🎉
Send **"hi"** from your phone to the test number.
Your terminal prints `[in] ...` and `[out] ...`, and you get a reply on WhatsApp.

---

## Practice mode: chat without WhatsApp (use this while Meta has the account locked)

1. In `.env`, set `SIMULATE=true` (tokens can stay empty).
2. Terminal 1 – start the server:
   ```
   uvicorn app.main:app --reload --port 8000
   ```
3. Terminal 2 – start the simulator (same folder):
   ```
   python simulate.py
   ```
4. Type messages like `hi` or `Suresh 650 credit`. The simulator sends them to your
   server in exactly the same format Meta uses, and prints the server's reply.

When Meta unlocks your account: set `SIMULATE=false`, fill in the tokens, and the
same code talks to real WhatsApp. Nothing else changes.

---

## Common problems

| Problem | Fix |
|---|---|
| `Missing in .env file` | `.env` not created, or a value is empty |
| Verify fails in Meta | ngrok not running, wrong URL (must end with `/webhook`), or verify word doesn't match |
| Message arrives but no reply, terminal shows `FAILED 401` | Token expired (temporary token lasts ~24 h) – copy a new one into `.env` |
| `FAILED 400` with "recipient not in allowed list" | Add your number as a test recipient in API Setup |
| Nothing arrives at all | Didn't subscribe to `messages` in Webhook fields |
| ngrok address changed | Free ngrok gives a new address each restart – update it in Meta's webhook settings |

---

## Check your understanding (answer without looking)

1. Why do we reply "200 OK" to Meta immediately and do the work in the background?
2. What is the `VERIFY_TOKEN` for, and who else knows it?
3. Why do we save the `wa_msg_id` of every incoming message?
4. Where does the access token go in the request to Meta?
5. Why is `.env` never uploaded to GitHub?
6. What does ngrok do, and why won't we need it after deploying to the cloud?

Send me your answers – I'll check them before we start Week 2.
# getlekka-dost-ai
