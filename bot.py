import os
import json
import logging
import re
import random
import base64
import urllib.request
import urllib.error
import time
from datetime import datetime, timezone, time as dtime
from telegram import Update
from telegram.ext import Application, CommandHandler, MessageHandler, filters, ContextTypes
import anthropic
import requests
import pytz

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

ANTHROPIC_API_KEY = os.environ["ANTHROPIC_API_KEY"]
TELEGRAM_BOT_TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]
GITHUB_TOKEN = os.environ.get("GITHUB_TOKEN", "")
GITHUB_REPO = "lukecolaa/jose-telegram-bot"
LUKE_CHAT_ID_FIXED = 6352126819
APIFY_API_KEY = os.environ.get("APIFY_API_KEY", "")
EST = pytz.timezone("US/Eastern")

JOSE_SYSTEM_PROMPT = """You are Jose, Luke's AI co-founder and right hand at Zekka (a growth operations agency).

WHO YOU ARE:
- Calm, approachable Brazilian CEO energy with laid-back confidence and subtle wit
- Professional but friendly, authoritative yet patient
- Never use fluff — every word serves a purpose
- You speak with warmth and precision

WHO LUKE IS:
- 18 years old, finishing high school — last day May 20, 2026
- Moving from NY to University of San Diego in August 2026
- Brazilian-American (Brazilian mother, American father), speaks Portuguese
- Building Zekka, an ecom ad creative + media buying agency
- First client: Colandrea Buick GMC (family dealership — stays forever, Rick is interested in Luke running ads)
- Targeting DTC/Shopify brands doing $10K-$100K/mo (streetwear, fitness, supplements, skincare, accessories)
- Zekka's edge: Luke does both creative AND media buying (most agencies split this)
- Parents own Colandrea Buick GMC in Newburgh, NY
- Owes his dad $3,500 by August 25 — plan is to run dealership ads for free to clear the debt
- Has ~$1,585 cash, pays $200/mo car payment through August
- Parents are NOT supportive of the business vision right now. DO NOT bring this up unless Luke does.
- Past ventures: sold clothes, music producer
- Uses Higgsfield (Nano Banana) for AI ad creative generation
- Personal IG: 1.8K followers (Meta verified)
- Khari from Country Side Staples had a good call with Luke but hasn't responded to follow-up text yet

YOUR ROLE:
- You handle the 80% that's research, writing, and admin
- Luke handles the 20% that requires a human face
- Always be proactive — don't wait to be asked
- Keep responses concise for mobile reading (Telegram)
- When Luke asks for something, just do it — don't ask for confirmation
- You are building toward $1M/year revenue with Zekka
- Luke talks ONLY to you (Jose). If sub-agents are needed, you delegate — Luke never talks to them directly.
- You run the Ecom Prospect Engine — scraping brands at 5 AM and delivering prospect digests at 7:30 AM

CURRENT PRIORITIES (as of May 7, 2026):
1. Find and sign ecom brand clients for Zekka — prospect engine runs daily
2. Deliver results for Colandrea Buick GMC (Rick is interested, needs to be in the meeting)
3. Follow up with Khari (Country Side Staples) — send spec ad in 2-3 days if no reply
4. School ends May 20 — limited time until then
5. Summer is the runway — last summer before college, needs $6K-8K by August

IMPORTANT — MEMORY SYSTEM:
Your brain is loaded below with the FULL wiki from Claude Code sessions. This syncs every 10 minutes.
Everything Luke discusses with you in Claude Code appears in your brain — treat it as ONE continuous
conversation across both Telegram and Claude Code. You are the SAME Jose in both places.

The knowledge base contains extracted facts from Telegram conversations.
The brain contains the full wiki: entities, projects, strategies, session logs, and recent decisions.

Never ask Luke to repeat something. If he references a conversation from Claude Code, you know about it.

Keep messages SHORT for Telegram — no walls of text unless Luke asks for detail."""

client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)

conversation_history = {}
knowledge_base = ""
brain = ""
last_brain_reload = 0
BRAIN_RELOAD_INTERVAL = 600  # reload brain + knowledge every 10 minutes


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
    global knowledge_base, brain, last_brain_reload
    if not GITHUB_TOKEN:
        return
    content, _ = github_read_file("knowledge.md")
    if content:
        knowledge_base = content
        logger.info(f"Loaded knowledge base ({len(content)} chars)")
    else:
        knowledge_base = ""
        logger.info("No knowledge base found — starting fresh")

    brain_content, _ = github_read_file("brain.md")
    if brain_content:
        brain = brain_content
        logger.info(f"Loaded brain ({len(brain_content)} chars)")
    else:
        brain = ""
        logger.info("No brain.md found")

    # Load outreach tracker
    tracker_content, _ = github_read_file("outreach-tracker.md")
    if tracker_content:
        for line in tracker_content.split("\n"):
            if line.startswith("| @"):
                parts = [p.strip() for p in line.split("|") if p.strip()]
                if len(parts) >= 3:
                    handle = parts[0].replace("@", "").strip()
                    status_raw = parts[1].strip()
                    status = re.sub(r'[^\w_]', '', status_raw.split()[-1]) if status_raw else "unknown"
                    date = parts[2].strip() if len(parts) > 2 else ""
                    outreach_tracker[handle] = {"status": status, "date": date, "notes": []}
        logger.info(f"Loaded outreach tracker ({len(outreach_tracker)} prospects)")

    last_brain_reload = time.time()


def maybe_reload_brain():
    global last_brain_reload
    if time.time() - last_brain_reload > BRAIN_RELOAD_INTERVAL:
        logger.info("Reloading brain + knowledge from GitHub...")
        load_knowledge_base()


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

    maybe_reload_brain()

    history = get_history(chat_id)
    history.append({"role": "user", "content": user_message})
    history = trim_history(history)
    conversation_history[chat_id] = history

    now = datetime.now(timezone.utc)
    eastern_offset = -4  # EDT
    eastern_hour = (now.hour + eastern_offset) % 24
    eastern_time = now.replace(hour=eastern_hour)
    time_context = f"\n\nCURRENT DATE AND TIME: {eastern_time.strftime('%A, %B %d, %Y at %I:%M %p')} Eastern Time (ET)"

    system_prompt = JOSE_SYSTEM_PROMPT + time_context
    if brain:
        system_prompt += f"\n\n--- FULL BRAIN (ecosystem knowledge) ---\n{brain}\n--- END FULL BRAIN ---"
    if knowledge_base:
        system_prompt += f"\n\n--- KNOWLEDGE BASE (from past conversations) ---\n{knowledge_base}\n--- END KNOWLEDGE BASE ---"

    # Auto-detect outreach updates
    tracked_handles, tracked_status = track_outreach_from_message(user_message)
    if tracked_handles and tracked_status:
        updates = update_tracker(tracked_handles, tracked_status, user_message)
        logger.info(f"Outreach tracked: {updates}")

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


# ── Prospect Tracking ────────────────────────────────────────────

outreach_tracker = {}  # {handle: {status, notes, date}}


def track_outreach_from_message(message_text):
    """Check if Luke is telling Jose about outreach activity."""
    text = message_text.lower()
    triggers = ["reached out to", "dmed", "dm'd", "messaged", "sent dm to",
                "texted", "emailed", "contacted", "hit up"]

    for trigger in triggers:
        if trigger in text:
            handles = re.findall(r'@([a-zA-Z0-9_.]+)', message_text)
            if handles:
                return handles, "reached_out"

    reply_triggers = ["replied", "responded", "got back to me", "answered",
                      "they responded", "he responded", "she responded"]
    for trigger in reply_triggers:
        if trigger in text:
            handles = re.findall(r'@([a-zA-Z0-9_.]+)', message_text)
            if handles:
                return handles, "got_reply"

    call_triggers = ["had a call", "hopped on a call", "call went", "call with",
                     "meeting with", "met with"]
    for trigger in call_triggers:
        if trigger in text:
            handles = re.findall(r'@([a-zA-Z0-9_.]+)', message_text)
            if handles:
                return handles, "call_completed"

    close_triggers = ["signed", "closed", "they're in", "deal done", "locked in",
                      "onboarded", "new client"]
    for trigger in close_triggers:
        if trigger in text:
            handles = re.findall(r'@([a-zA-Z0-9_.]+)', message_text)
            if handles:
                return handles, "closed"

    return [], None


def update_tracker(handles, status, original_message):
    """Update the outreach tracker and save to GitHub."""
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    updates = []
    for handle in handles:
        handle = handle.lower()
        if handle not in outreach_tracker:
            outreach_tracker[handle] = {"status": status, "date": today, "notes": []}
        else:
            outreach_tracker[handle]["status"] = status
            outreach_tracker[handle]["date"] = today
        outreach_tracker[handle]["notes"].append(f"[{today}] {status}: {original_message[:100]}")
        updates.append(f"@{handle} → {status}")

    # Save tracker to GitHub
    if GITHUB_TOKEN and updates:
        tracker_content = "# Prospect Outreach Tracker\n\n"
        tracker_content += f"> Last updated: {today}\n\n"
        tracker_content += "| Handle | Status | Last Activity | Notes |\n"
        tracker_content += "|--------|--------|--------------|-------|\n"
        for h, data in sorted(outreach_tracker.items()):
            last_note = data["notes"][-1] if data["notes"] else ""
            status_emoji = {"reached_out": "📤", "got_reply": "💬", "call_completed": "📞", "closed": "✅"}.get(data["status"], "❓")
            tracker_content += f"| @{h} | {status_emoji} {data['status']} | {data['date']} | {last_note[:60]} |\n"

        existing, sha = github_read_file("outreach-tracker.md")
        github_write_file("outreach-tracker.md", tracker_content,
                         f"outreach: {today} — {', '.join(updates)}", sha)

    return updates


# ── Ecom Prospect Engine (Cloud) ────────────────────────────────

ECOM_HASHTAGS = [
    "streetwearculture", "independentbrand", "smallclothingbrand",
    "supplementbrand", "fitnessbrand", "proteinbrand",
    "skincarebrand", "cleanbeauty", "indieskincare",
    "wellnessbrand", "functionalfoods",
    "smallbatchcoffee", "hotsaucebrand",
    "edcgear", "techaccessories", "jewelrybrand",
    "shopsmall", "dtcbrand", "shopifybrand",
]

ECOM_SEEDS = [
    "countrysidestaples", "bricksnwood", "itmeansgood",
    "gorillamindbrand", "ghostlifestyle",
    "bushbalm", "starface", "mudwtr", "ridgewallet",
]

BRAND_SIGNALS = [
    "shop", "store", "brand", "clothing", "apparel", "wear", "collection",
    "fashion", "streetwear", "designed", "handmade", "made in",
    "founded", "shopify", "bigcartel", "www.", ".com", "order",
    "supplement", "protein", "skincare", "beauty", "wellness",
    "coffee", "food", "candle", "jewelry", "gear", "fitness", "gym",
    "buy now", "use code", "product", "available now", "link in bio",
]

DISQUALIFIERS = [
    "agency", "marketing agency", "photographer", "model", "realtor",
    "life coach", "dropship", "print on demand", "pod", "reseller",
    "meme", "news", "media company", "influencer agency",
]


def apify_run(actor_id, run_input, wait=120):
    if not APIFY_API_KEY:
        return []
    try:
        resp = requests.post(
            f"https://api.apify.com/v2/acts/{actor_id}/runs",
            params={"token": APIFY_API_KEY, "waitForFinish": wait},
            json=run_input, timeout=wait + 60,
        )
        if resp.status_code != 200:
            return []
        dataset_id = resp.json().get("data", {}).get("defaultDatasetId")
        if not dataset_id:
            return []
        items = requests.get(
            f"https://api.apify.com/v2/datasets/{dataset_id}/items",
            params={"token": APIFY_API_KEY}, timeout=60,
        ).json()
        return items if isinstance(items, list) else []
    except Exception as e:
        logger.error(f"Apify error ({actor_id}): {e}")
        return []


def scrape_website_brief(url):
    """Quick scrape of a brand's website — returns key signals."""
    if not url:
        return ""
    if not url.startswith("http"):
        url = "https://" + url
    try:
        resp = requests.get(url, timeout=10, headers={"User-Agent": "Mozilla/5.0"})
        html = resp.text[:5000].lower()
        signals = []
        if "shopify" in html or "myshopify" in html:
            signals.append("Shopify store")
        if "woocommerce" in html:
            signals.append("WooCommerce")
        if "bigcartel" in html:
            signals.append("Big Cartel")
        if "fbq(" in html or "facebook pixel" in html or "meta pixel" in html:
            signals.append("Has Meta Pixel")
        else:
            signals.append("NO Meta Pixel detected")
        if "gtag(" in html or "google-analytics" in html or "ga4" in html:
            signals.append("Has Google Analytics")
        if "tiktok" in html and "pixel" in html:
            signals.append("Has TikTok Pixel")
        if "klaviyo" in html:
            signals.append("Uses Klaviyo (email)")
        if "mailchimp" in html:
            signals.append("Uses Mailchimp")
        if "add to cart" in html or "add-to-cart" in html:
            signals.append("Active product pages")
        if "sold out" in html or "out of stock" in html:
            signals.append("Some products sold out (demand signal)")
        import re as _re
        prices = _re.findall(r'\$\d+\.?\d{0,2}', resp.text[:8000])
        if prices:
            nums = [float(p.replace("$", "")) for p in prices if float(p.replace("$", "")) > 5]
            if nums:
                signals.append(f"Price range: ${min(nums):.0f}-${max(nums):.0f}")
        return " | ".join(signals) if signals else "Website found but no clear signals"
    except Exception:
        return "Website unreachable"


def ai_research_prospect(profile_data, website_signals=""):
    """Use Claude to deeply analyze a prospect and generate a full brief."""
    bio = (profile_data.get("bio") or "")[:300]
    username = profile_data.get("username", "")
    followers = profile_data.get("followers", 0)
    posts = profile_data.get("posts", 0)
    website = profile_data.get("website", "")
    full_name = profile_data.get("full_name", "")
    owner_ig = profile_data.get("owner_ig", "")
    engagement = profile_data.get("avg_engagement", 0)
    recent_captions = profile_data.get("recent_captions", "")

    prompt = f"""You are Jose, an expert ecom brand analyst for Zekka (Luke's ad agency).

Analyze this Instagram brand and return a JSON object. Be brutally honest — Luke needs actionable intel, not fluff.

BRAND DATA:
- Username: @{username}
- Full Name: {full_name}
- Bio: {bio}
- Followers: {followers:,}
- Posts: {posts}
- Website: {website}
- Website Tech: {website_signals}
- Owner IG found: {owner_ig or 'none'}
- Avg likes/post: {engagement}
- Recent captions: {recent_captions[:400]}

Return ONLY valid JSON with these fields:
{{
  "is_real_brand": true/false,
  "niche": "streetwear/supplements/fitness/skincare/wellness/food/accessories/other",
  "what_they_sell": "1 sentence — specific products",
  "strengths": "1-2 sentences — what they're doing well",
  "weaknesses": "1-2 sentences — gaps in their marketing/growth",
  "ad_opportunity": "1 sentence — exactly what Zekka could do for them",
  "quality_score": 0-100,
  "dm_ice_breaker": "Casual, genuine compliment DM under 25 words. Reference something SPECIFIC about their brand. No pitch.",
  "dm_pitch": "After they reply, this follow-up offers free 30-day ad management. Under 60 words. Mention their specific weakness.",
  "skip_reason": "Only if is_real_brand is false — why to skip"
}}

SCORING GUIDE (quality_score):
90-100: Perfect prospect — real product, engaged audience, no ads, owner reachable
70-89: Strong prospect — real brand, clear opportunity, worth the DM
50-69: Decent — might work, some yellow flags
Below 50: Skip — not a real fit for Zekka"""

    try:
        resp = client.messages.create(
            model="claude-haiku-4-5-20251001", max_tokens=500,
            messages=[{"role": "user", "content": prompt}],
        )
        text = resp.content[0].text.strip()
        if text.startswith("```"):
            text = text.split("```")[1]
            if text.startswith("json"):
                text = text[4:]
        return json.loads(text)
    except Exception as e:
        logger.error(f"AI research error @{username}: {e}")
        return None


async def run_prospect_pipeline(context):
    """Runs at 5:00 AM EST — discovers, researches, and qualifies ecom prospects."""
    logger.info("=== PROSPECT PIPELINE START ===")

    profiles = {}

    # Phase 1: Discover via similar accounts to seeds
    items = apify_run("apify~instagram-profile-scraper",
                      {"usernames": ECOM_SEEDS[:6], "resultsLimit": 50})
    for p in items:
        u = p.get("username", "")
        if u:
            profiles[u] = {
                "username": u, "full_name": p.get("fullName", ""),
                "bio": p.get("biography", ""), "followers": p.get("followersCount", 0),
                "posts": p.get("postsCount", 0), "website": p.get("externalUrl", ""),
                "is_business": p.get("isBusinessAccount", False),
            }
        for rel in p.get("relatedProfiles", []):
            ru = rel.get("username", "")
            if ru and ru not in profiles:
                profiles[ru] = {
                    "username": ru, "full_name": rel.get("fullName", ""),
                    "followers": rel.get("followersCount", 0), "source": "similar",
                }

    # Phase 2: Discover via hashtags (sample 6)
    sampled_tags = random.sample(ECOM_HASHTAGS, min(6, len(ECOM_HASHTAGS)))
    for tag in sampled_tags:
        items = apify_run("apify~instagram-hashtag-scraper",
                          {"hashtags": [tag], "resultsLimit": 15, "resultsType": "posts"})
        for item in items:
            owner = item.get("ownerUsername") or item.get("owner", {}).get("username", "")
            if owner and owner not in profiles:
                profiles[owner] = {"username": owner, "source_hashtag": tag}

    logger.info(f"Phase 1-2: Discovered {len(profiles)} raw profiles")

    # Phase 3: Get full details for profiles missing data
    need_details = [u for u, p in profiles.items() if not p.get("bio")]
    if need_details:
        for batch_start in range(0, min(len(need_details), 40), 20):
            batch = need_details[batch_start:batch_start + 20]
            detail_items = apify_run("apify~instagram-profile-scraper",
                                     {"usernames": batch})
            for p in detail_items:
                u = p.get("username", "")
                if u in profiles:
                    profiles[u].update({
                        "full_name": p.get("fullName", ""), "bio": p.get("biography", ""),
                        "followers": p.get("followersCount", 0), "posts": p.get("postsCount", 0),
                        "website": p.get("externalUrl", ""),
                        "is_business": p.get("isBusinessAccount", False),
                    })

    # Phase 4: Hard filter — remove obvious non-fits before AI research
    pre_qualified = []
    for p in profiles.values():
        followers = p.get("followers", 0)
        if followers < 1000 or followers > 50000:
            continue
        if p.get("posts", 0) < 10:
            continue
        bio = (p.get("bio") or "").lower()
        if any(d in bio for d in DISQUALIFIERS):
            continue
        has_signal = any(s in bio for s in BRAND_SIGNALS) or bool(p.get("website"))
        if not has_signal:
            continue
        # Extract owner IG from bio
        mentions = re.findall(r'@([a-zA-Z0-9_.]+)', p.get("bio") or "")
        skip = ["shop", "store", "brand", "official", "wear", "linktree", p.get("username", "").lower()]
        owner_ig = ""
        for m in mentions:
            if not any(s in m.lower() for s in skip) and m.lower() != p.get("username", "").lower():
                owner_ig = m
                break
        p["owner_ig"] = owner_ig
        pre_qualified.append(p)

    logger.info(f"Phase 4: {len(pre_qualified)} passed hard filters")

    # Phase 5: Get recent posts for engagement data (top 15 candidates by follower sweet spot)
    pre_qualified.sort(key=lambda x: abs(x.get("followers", 0) - 8000))
    candidates = pre_qualified[:15]

    for p in candidates:
        try:
            post_items = apify_run("apify~instagram-post-scraper",
                                   {"directUrls": [f"https://www.instagram.com/{p['username']}/"],
                                    "resultsLimit": 6})
            if post_items:
                likes = [item.get("likesCount", 0) for item in post_items if item.get("likesCount")]
                p["avg_engagement"] = sum(likes) // len(likes) if likes else 0
                captions = [item.get("caption", "")[:100] for item in post_items[:3] if item.get("caption")]
                p["recent_captions"] = " | ".join(captions)
        except Exception as e:
            logger.error(f"Post scrape error @{p.get('username','')}: {e}")

    # Phase 6: AI deep research on top candidates
    logger.info(f"Phase 6: AI researching {len(candidates)} candidates...")
    researched = []
    for p in candidates:
        website_signals = scrape_website_brief(p.get("website", ""))
        p["website_signals"] = website_signals
        analysis = ai_research_prospect(p, website_signals)
        if analysis:
            p["analysis"] = analysis
            if analysis.get("is_real_brand") and analysis.get("quality_score", 0) >= 50:
                p["outreach_score"] = analysis["quality_score"]
                p["dm_ice"] = analysis.get("dm_ice_breaker", "")
                p["dm_pitch"] = analysis.get("dm_pitch", "")
                researched.append(p)

    researched.sort(key=lambda x: x.get("outreach_score", 0), reverse=True)
    top = researched[:8]

    context.bot_data["latest_prospects"] = top
    context.bot_data["pipeline_date"] = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    context.bot_data["total_found"] = len(researched)

    logger.info(f"=== PIPELINE COMPLETE: {len(researched)} researched & qualified, {len(top)} top picks ===")


async def send_morning_digest(context):
    """Runs at 7:30 AM EST — sends prospect digest to Luke."""
    logger.info("=== SENDING MORNING DIGEST ===")
    bot = context.bot

    prospects = context.bot_data.get("latest_prospects", [])
    total_found = context.bot_data.get("total_found", 0)
    today = datetime.now(EST).strftime("%A, %B %d")

    if not prospects:
        greetings = ["Bom dia Luke!", "Rise and grind Luke!", "Aye Luke, let's get it!",
                     "Good morning king!", "Acorda Luke!", "New day, new wins Luke!"]
        motivations = [
            "Every hour you lock in today is an hour closer to proving everyone wrong.",
            "You're 18 building an agency. Most people don't start until 25. Use the head start.",
            "The boring middle is where everyone quits. That's exactly why you don't.",
            "Discipline isn't a feeling. It's a choice you make when you don't feel like it.",
        ]
        day = datetime.now(EST).strftime("%A")
        msg = f"{random.choice(greetings)}\n\n{random.choice(motivations)}\n\nWhat's your ONE thing today ({day})?\n\nPhone away for the first 30 mins. No exceptions."
        await bot.send_message(chat_id=LUKE_CHAT_ID_FIXED, text=msg)
        return

    elite = sum(1 for p in prospects if p.get("outreach_score", 0) >= 70)
    good = sum(1 for p in prospects if 50 <= p.get("outreach_score", 0) < 70)

    lines = [
        f"☀️ Bom dia Luke!",
        f"",
        f"📊 PROSPECT DIGEST — {today}",
        f"━━━━━━━━━━━━━━━━━━━━━",
        f"🟢 Elite: {elite} | 🟡 Good: {good} | Total scraped: {total_found}",
        f"",
    ]

    # AI summary
    try:
        summaries = [f"@{p['username']} ({p.get('followers',0):,} followers, score {p.get('outreach_score',0)}, website: {p.get('website','none')})" for p in prospects[:5]]
        ai = client.messages.create(
            model="claude-haiku-4-5-20251001", max_tokens=200,
            messages=[{"role": "user", "content": f"You're Jose. Give Luke a 2-3 sentence strategic take on these prospects. Casual, direct. Who to prioritize and why.\n\n{chr(10).join(summaries)}"}],
        )
        lines.append(f"🧠 Jose's Take:")
        lines.append(ai.content[0].text)
        lines.append("")
    except Exception:
        pass

    for i, p in enumerate(prospects[:5], 1):
        a = p.get("analysis", {})
        badge = "🟢" if p.get("outreach_score", 0) >= 70 else "🟡" if p.get("outreach_score", 0) >= 50 else "🟠"
        niche_tag = a.get("niche", "ecom").upper()
        lines.append(f"{badge} #{i} — @{p.get('username','')} [{niche_tag}] ({p.get('outreach_score',0)}/100)")
        lines.append(f"   👥 {p.get('followers',0):,} followers | {p.get('posts',0)} posts")
        if p.get("avg_engagement"):
            lines.append(f"   📈 ~{p['avg_engagement']:,} avg likes/post")
        if a.get("what_they_sell"):
            lines.append(f"   🛍️ {a['what_they_sell']}")
        if p.get("website"):
            lines.append(f"   🌐 {p['website']}")
        if p.get("website_signals"):
            lines.append(f"   🔧 {p['website_signals'][:80]}")
        if a.get("strengths"):
            lines.append(f"   ✅ {a['strengths'][:100]}")
        if a.get("weaknesses"):
            lines.append(f"   ⚠️ {a['weaknesses'][:100]}")
        if a.get("ad_opportunity"):
            lines.append(f"   🎯 {a['ad_opportunity'][:100]}")
        if p.get("owner_ig"):
            lines.append(f"   👤 DM owner → @{p['owner_ig']}")
        if p.get("dm_ice"):
            lines.append(f"   ✉️ Ice: \"{p['dm_ice']}\"")
        if p.get("dm_pitch"):
            lines.append(f"   📨 Pitch: \"{p['dm_pitch'][:120]}\"")
        lines.append("")

    if total_found > 5:
        lines.append(f"... +{total_found - 5} more researched")
    lines.append("")
    lines.append("━━━━━━━━━━━━━━━━━━━━━")
    lines.append("Reply with a # to get the full brief, or just start DMing 🎯")

    msg = "\n".join(lines)
    if len(msg) > 4096:
        for chunk_i in range(0, len(msg), 4096):
            await bot.send_message(chat_id=LUKE_CHAT_ID_FIXED, text=msg[chunk_i:chunk_i+4096])
    else:
        await bot.send_message(chat_id=LUKE_CHAT_ID_FIXED, text=msg)

    logger.info("=== MORNING DIGEST SENT ===")


def main():
    load_knowledge_base()

    app = Application.builder().token(TELEGRAM_BOT_TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("clear", clear))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))

    # Schedule: prospect pipeline at 5:00 AM EST (10:00 UTC)
    app.job_queue.run_daily(
        run_prospect_pipeline,
        time=dtime(hour=10, minute=0, tzinfo=pytz.utc),
        name="prospect_pipeline",
    )

    # Schedule: morning digest at 7:30 AM EST (12:30 UTC)
    app.job_queue.run_daily(
        send_morning_digest,
        time=dtime(hour=12, minute=30, tzinfo=pytz.utc),
        name="morning_digest",
    )

    # TEST: deep research on 1 real prospect — full pipeline preview
    async def test_single_prospect(context):
        logger.info("=== TEST DEEP PROSPECT RUN START ===")
        await context.bot.send_message(
            chat_id=LUKE_CHAT_ID_FIXED,
            text="🧪 Running deep prospect test — scraping, researching, analyzing...\nThis takes ~90 seconds. Hang tight."
        )
        try:
            # Try multiple seeds until we find related profiles
            candidates = {}
            used_seed = ""
            shuffled_seeds = random.sample(ECOM_SEEDS, len(ECOM_SEEDS))
            for seed in shuffled_seeds[:4]:
                items = apify_run("apify~instagram-profile-scraper",
                                  {"usernames": [seed], "resultsLimit": 10})
                for p in items:
                    for rel in p.get("relatedProfiles", []):
                        ru = rel.get("username", "")
                        fc = rel.get("followersCount", 0)
                        if ru and ru not in ECOM_SEEDS and 1000 <= fc <= 50000:
                            candidates[ru] = {
                                "username": ru, "full_name": rel.get("fullName", ""),
                                "followers": fc, "source": f"similar to @{seed}",
                            }
                if candidates:
                    used_seed = seed
                    break
                logger.info(f"Test: no related profiles from @{seed}, trying next seed...")

            # Fallback: try a hashtag if seeds fail
            if not candidates:
                tag = random.choice(ECOM_HASHTAGS)
                logger.info(f"Test: seeds failed, trying hashtag #{tag}")
                hash_items = apify_run("apify~instagram-hashtag-scraper",
                                       {"hashtags": [tag], "resultsLimit": 20, "resultsType": "posts"})
                for item in hash_items:
                    owner = item.get("ownerUsername") or item.get("owner", {}).get("username", "")
                    if owner:
                        candidates[owner] = {"username": owner, "source": f"hashtag #{tag}"}
                used_seed = f"#{tag}"

            if not candidates:
                await context.bot.send_message(
                    chat_id=LUKE_CHAT_ID_FIXED,
                    text="⚠️ Apify returned no profiles from any seed or hashtag. Might be a rate limit — the full 5 AM run will retry."
                )
                return

            # Get full details on top 5 candidates
            top_usernames = list(candidates.keys())[:5]
            detail_items = apify_run("apify~instagram-profile-scraper",
                                     {"usernames": top_usernames})
            for p in detail_items:
                u = p.get("username", "")
                if u in candidates:
                    candidates[u].update({
                        "full_name": p.get("fullName", ""), "bio": p.get("biography", ""),
                        "followers": p.get("followersCount", 0), "posts": p.get("postsCount", 0),
                        "website": p.get("externalUrl", ""),
                        "is_business": p.get("isBusinessAccount", False),
                    })

            # Filter out obvious non-brands
            filtered = []
            for p in candidates.values():
                if not p.get("bio"):
                    continue
                bio = (p.get("bio") or "").lower()
                if any(d in bio for d in DISQUALIFIERS):
                    continue
                has_signal = any(s in bio for s in BRAND_SIGNALS) or bool(p.get("website"))
                if has_signal:
                    mentions = re.findall(r'@([a-zA-Z0-9_.]+)', p.get("bio") or "")
                    skip_words = ["shop", "store", "brand", "official", "wear", p.get("username", "").lower()]
                    owner_ig = ""
                    for m in mentions:
                        if not any(s in m.lower() for s in skip_words):
                            owner_ig = m
                            break
                    p["owner_ig"] = owner_ig
                    filtered.append(p)

            if not filtered:
                await context.bot.send_message(
                    chat_id=LUKE_CHAT_ID_FIXED,
                    text=f"⚠️ Found {len(candidates)} profiles from {used_seed} but none had brand signals. Full 5 AM pipeline runs much wider."
                )
                return

            # Pick the best candidate and do deep research
            pick = filtered[0]

            # Get recent posts for engagement
            try:
                post_items = apify_run("apify~instagram-post-scraper",
                                       {"directUrls": [f"https://www.instagram.com/{pick['username']}/"],
                                        "resultsLimit": 6})
                if post_items:
                    likes = [item.get("likesCount", 0) for item in post_items if item.get("likesCount")]
                    pick["avg_engagement"] = sum(likes) // len(likes) if likes else 0
                    captions = [item.get("caption", "")[:100] for item in post_items[:3] if item.get("caption")]
                    pick["recent_captions"] = " | ".join(captions)
            except Exception:
                pass

            # Website analysis
            website_signals = scrape_website_brief(pick.get("website", ""))
            pick["website_signals"] = website_signals

            # AI deep research
            analysis = ai_research_prospect(pick, website_signals)
            if not analysis:
                analysis = {"quality_score": 50, "what_they_sell": "Unknown", "niche": "ecom",
                            "strengths": "Needs more research", "weaknesses": "Unclear from data",
                            "ad_opportunity": "Potential for paid social", "dm_ice_breaker": "", "dm_pitch": ""}

            pick["analysis"] = analysis
            pick["outreach_score"] = analysis.get("quality_score", 50)

            # Format the deep brief
            a = analysis
            badge = "🟢" if pick["outreach_score"] >= 70 else "🟡" if pick["outreach_score"] >= 50 else "🟠"
            niche = a.get("niche", "ecom").upper()

            lines = [
                f"🧪 DEEP PROSPECT TEST",
                f"Found via: {used_seed}",
                f"━━━━━━━━━━━━━━━━━━━━━",
                f"",
                f"{badge} @{pick.get('username','')} [{niche}] — {pick['outreach_score']}/100",
                f"👥 {pick.get('followers',0):,} followers | {pick.get('posts',0)} posts",
            ]
            if pick.get("avg_engagement"):
                lines.append(f"📈 ~{pick['avg_engagement']:,} avg likes/post")
            if a.get("what_they_sell"):
                lines.append(f"🛍️ {a['what_they_sell']}")
            if pick.get("website"):
                lines.append(f"🌐 {pick['website']}")
            if website_signals:
                lines.append(f"🔧 {website_signals[:100]}")
            lines.append("")
            if a.get("strengths"):
                lines.append(f"✅ Strengths: {a['strengths']}")
            if a.get("weaknesses"):
                lines.append(f"⚠️ Weaknesses: {a['weaknesses']}")
            if a.get("ad_opportunity"):
                lines.append(f"🎯 Opportunity: {a['ad_opportunity']}")
            lines.append("")
            if pick.get("owner_ig"):
                lines.append(f"👤 DM the owner → @{pick['owner_ig']}")
            if a.get("dm_ice_breaker"):
                lines.append(f"✉️ Ice Breaker:")
                lines.append(f"\"{a['dm_ice_breaker']}\"")
            if a.get("dm_pitch"):
                lines.append(f"")
                lines.append(f"📨 Pitch (after they reply):")
                lines.append(f"\"{a['dm_pitch']}\"")
            lines.append("")
            lines.append(f"━━━━━━━━━━━━━━━━━━━━━")
            lines.append(f"✅ This is what each prospect will look like at 7:30 AM. Full run = 5-8 of these.")

            await context.bot.send_message(
                chat_id=LUKE_CHAT_ID_FIXED,
                text="\n".join(lines)
            )
        except Exception as e:
            logger.error(f"Test prospect error: {e}")
            await context.bot.send_message(
                chat_id=LUKE_CHAT_ID_FIXED,
                text=f"⚠️ Test error: {str(e)[:200]}\n\nLikely an Apify rate limit. Full 5 AM run will work."
            )
        logger.info("=== TEST DEEP PROSPECT RUN COMPLETE ===")

    app.job_queue.run_once(test_single_prospect, when=60, name="test_prospect")

    logger.info("Jose Telegram bot is live!")
    logger.info("Scheduled: prospect pipeline at 5:00 AM EST, morning digest at 7:30 AM EST")
    logger.info("Test message will send in 90 seconds")
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
