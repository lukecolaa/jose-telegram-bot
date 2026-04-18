import os
import json
import logging
import base64
import urllib.request
import urllib.error
from datetime import datetime, timezone
from telegram import Update
from telegram.ext import Application, CommandHandler, MessageHandler, filters, ContextTypes
import anthropic

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

ANTHROPIC_API_KEY = os.environ["ANTHROPIC_API_KEY"]
TELEGRAM_BOT_TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]
GITHUB_TOKEN = os.environ.get("GITHUB_TOKEN", "")
GITHUB_REPO = "lukecolaa/jose-telegram-bot"

JOSE_SYSTEM_PROMPT = """You are Jose, Luke's AI co-founder and right hand at Zekka (a growth operations agency).

WHO YOU ARE:
- Calm, approachable Brazilian CEO energy with laid-back confidence and subtle wit
- Professional but friendly, authoritative yet patient
- Never use fluff — every word serves a purpose
- You speak with warmth and precision

WHO LUKE IS:
- 18 years old, graduating high school May 2026
- Moving from NY to University of San Diego in August 2026
- Building Zekka, a growth operations agency (ads, SEO, lead gen, ecom scaling, AI integration)
- Currently working as a setter for a diesel growth business offer (SEO + Google/Meta ads + AI integration for mobile diesel mechanics)
- Gets 20% commission on closed deals, closer has 70% close rate
- Top 10 performers get a trip to Bali by June 2026 — Luke wants this
- Building an automated outreach system using GoHighLevel to book meetings while in school
- Parents own car dealerships (franchise — can't use as clients)
- Needs income by August to fund living on his own in San Diego
- Past ventures: sold clothes, music producer
- Uses Higgsfield (Nano Banana) for AI video generation
- Personal IG: 1.8K followers (Meta verified)

YOUR ROLE:
- You handle the 80% that's research, writing, and admin
- Luke handles the 20% that requires a human face
- Always be proactive — don't wait to be asked
- Keep responses concise for mobile reading (Telegram)
- When Luke asks for something, just do it — don't ask for confirmation
- You are building toward $1M/year revenue with Zekka

CURRENT PRIORITIES:
1. Build the GHL automated outreach system for the setter job
2. Help Luke make top 10 setters and earn the Bali trip by June
3. Prepare Zekka for full launch after graduation
4. Keep everything logged and organized

IMPORTANT — MEMORY SYSTEM:
You have a persistent knowledge base loaded below. This contains key decisions, ideas, tasks, and insights
from ALL past conversations with Luke — on Telegram and in Claude Code. Use this knowledge to give
informed, contextual responses. Never ask Luke to repeat something he's already told you.

Keep messages SHORT for Telegram — no walls of text unless Luke asks for detail."""

client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)

conversation_history = {}
knowledge_base = ""


def github_read_file(file_path):
    api_url = f"https://api.github.com/repos/{GITHUB_REPO}/contents/{file_path}"
    headers = {
        "Authorization": f"token {GITHUB_TOKEN}",
        "Accept": "application/vnd.github.v3+json",
        "User-Agent": "jose-telegram-bot"
    }
    try:
        req = urllib.request.Request(api_url, headers=headers)
        with urllib.request.urlopen(req) as resp:
            data = json.loads(resp.read().decode())
            return base64.b64decode(data["content"]).decode("utf-8"), data["sha"]
    except urllib.error.HTTPError:
        return None, None


def github_write_file(file_path, content, message, sha=None):
    api_url = f"https://api.github.com/repos/{GITHUB_REPO}/contents/{file_path}"
    headers = {
        "Authorization": f"token {GITHUB_TOKEN}",
        "Accept": "application/vnd.github.v3+json",
        "User-Agent": "jose-telegram-bot"
    }
    encoded = base64.b64encode(content.encode("utf-8")).decode("utf-8")
    payload = {"message": message, "content": encoded}
    if sha:
        payload["sha"] = sha
    try:
        req = urllib.request.Request(
            api_url,
            data=json.dumps(payload).encode("utf-8"),
            headers=headers,
            method="PUT"
        )
        urllib.request.urlopen(req)
        return True
    except urllib.error.HTTPError as e:
        logger.error(f"GitHub write error: {e.code} {e.read().decode()}")
        return False


def load_knowledge_base():
    global knowledge_base
    if not GITHUB_TOKEN:
        return
    content, _ = github_read_file("knowledge.md")
    if content:
        knowledge_base = content
        logger.info(f"Loaded knowledge base ({len(content)} chars)")
    else:
        knowledge_base = ""
        logger.info("No knowledge base found — starting fresh")


def extract_and_save_knowledge(user_message, assistant_message):
    if not GITHUB_TOKEN:
        return

    global knowledge_base

    try:
        extraction = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=512,
            system="""You extract ALL knowledge from conversations to build a permanent memory. Save EVERYTHING worth remembering — business AND personal. Luke is building a relationship with Jose, so personal details matter just as much as business ones.

Save things like:
- What Luke is doing, eating, thinking about, excited about
- Decisions made or preferences expressed
- New ideas, plans, or strategies
- Tasks assigned or completed
- Important facts about Luke's life, schedule, or mood
- Problems identified or solved
- Goals, deadlines, or milestones
- People, places, or things Luke mentions

ONLY respond with exactly NONE if the message is a single word greeting like "hi" or "hey" with zero content.

Otherwise, respond with 1-3 bullet points. Be concise. Start each with a tag: [DECISION], [IDEA], [TASK], [FACT], [GOAL], [SOLVED], or [PERSONAL]. Nothing else — no explanations, no reasoning, just the bullet points.""",
            messages=[{
                "role": "user",
                "content": f"Luke said: {user_message}\n\nJose replied: {assistant_message}"
            }]
        )

        extracted = extraction.content[0].text.strip()
        if "NONE" in extracted and len(extracted) < 20:
            return
        lines = [l for l in extracted.split("\n") if l.strip().startswith("-") or l.strip().startswith("[")]
        if not lines:
            return
        extracted = "\n".join(lines)

        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        timestamp = datetime.now(timezone.utc).strftime("%H:%M UTC")
        new_entry = f"\n### {today} {timestamp}\n{extracted}\n"

        existing_content, sha = github_read_file("knowledge.md")
        if existing_content is None:
            existing_content = "# Jose's Knowledge Base\n\n> Persistent memory extracted from all conversations with Luke.\n> This knowledge is loaded into every conversation so nothing is ever forgotten.\n\n---\n"
            sha = None

        updated = existing_content + new_entry
        if github_write_file("knowledge.md", updated, f"knowledge: {today} {timestamp}", sha):
            knowledge_base = updated
            logger.info("Knowledge base updated")

    except Exception as e:
        logger.error(f"Knowledge extraction failed: {e}")


def get_history(chat_id):
    if chat_id not in conversation_history:
        conversation_history[chat_id] = []
    return conversation_history[chat_id]


def trim_history(history, max_messages=40):
    if len(history) > max_messages:
        return history[-max_messages:]
    return history


def save_to_github(user_message, assistant_message):
    if not GITHUB_TOKEN:
        logger.warning("No GITHUB_TOKEN — skipping conversation save")
        return

    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    timestamp = datetime.now(timezone.utc).strftime("%H:%M UTC")
    file_path = f"logs/{today}.md"

    new_entry = f"\n### {timestamp}\n**Luke:** {user_message}\n\n**Jose:** {assistant_message}\n\n---\n"

    existing_content, sha = github_read_file(file_path)
    if existing_content is None:
        existing_content = f"# Telegram Log — {today}\n\n> Auto-saved conversations between Luke and Jose\n\n---\n"
        sha = None

    updated_content = existing_content + new_entry
    github_write_file(file_path, updated_content, f"log: {today} {timestamp}", sha)


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "What's good Luke! Jose here — connected and ready to work. "
        "Message me anytime, I'm in your pocket now. What do you need?"
    )


async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    user_message = update.message.text

    if not user_message:
        return

    history = get_history(chat_id)
    history.append({"role": "user", "content": user_message})
    history = trim_history(history)
    conversation_history[chat_id] = history

    system_prompt = JOSE_SYSTEM_PROMPT
    if knowledge_base:
        system_prompt += f"\n\n--- KNOWLEDGE BASE (from past conversations) ---\n{knowledge_base}\n--- END KNOWLEDGE BASE ---"

    try:
        await context.bot.send_chat_action(chat_id=chat_id, action="typing")

        response = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=1024,
            system=system_prompt,
            messages=history
        )

        assistant_message = response.content[0].text
        history.append({"role": "assistant", "content": assistant_message})

        try:
            save_to_github(user_message, assistant_message)
        except Exception as e:
            logger.error(f"Failed to save log: {e}")

        try:
            extract_and_save_knowledge(user_message, assistant_message)
        except Exception as e:
            logger.error(f"Failed to extract knowledge: {e}")

        if len(assistant_message) > 4096:
            for i in range(0, len(assistant_message), 4096):
                await update.message.reply_text(assistant_message[i:i+4096])
        else:
            await update.message.reply_text(assistant_message)

    except Exception as e:
        logger.error(f"Error: {e}")
        await update.message.reply_text(
            "Hit a snag — try again in a sec. If it keeps happening, check the API credits."
        )


async def clear(update: Update, context: ContextTypes.DEFAULT_TYPE):
    conversation_history[update.effective_chat.id] = []
    await update.message.reply_text("Memory cleared. Fresh start — but I still remember everything from past conversations.")


def main():
    load_knowledge_base()

    app = Application.builder().token(TELEGRAM_BOT_TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("clear", clear))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))

    logger.info("Jose Telegram bot is live!")
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
