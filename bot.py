import os
import base64
import json
import logging
from io import BytesIO

import anthropic
import gspread
from google.oauth2.service_account import Credentials
from telegram import Update
from telegram.ext import ApplicationBuilder, MessageHandler, CommandHandler, ContextTypes, filters
from dotenv import load_dotenv

load_dotenv()

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger(__name__)

ALLOWED_USER_ID = int(os.environ["ALLOWED_USER_ID"])

SCOPES = ["https://www.googleapis.com/auth/spreadsheets", "https://www.googleapis.com/auth/drive.readonly"]
creds = Credentials.from_service_account_file(os.environ["GOOGLE_SHEETS_CREDS"], scopes=SCOPES)
gc = gspread.authorize(creds)
sheet = gc.open(os.environ["SPREADSHEET_NAME"]).worksheet("FoodDatabase")

claude = anthropic.Anthropic(api_key=os.environ["ANTHROPIC_API_KEY"])


def insert_row_in_table(worksheet, row):
    """Insert a row at the end of the table using insertDimension so that
    the native Table (or banded range) automatically applies its formatting."""
    spreadsheet_id = worksheet.spreadsheet.id
    sheet_id = worksheet._properties['sheetId']

    all_values = worksheet.get_all_values()
    last_data_row = len(all_values)       # 1-indexed last row with data
    new_row_index = last_data_row         # 0-indexed insert position (after last row)

    batch_url = f"https://sheets.googleapis.com/v4/spreadsheets/{spreadsheet_id}:batchUpdate"
    gc.http_client.request('post', batch_url, json={'requests': [{
        'insertDimension': {
            'range': {
                'sheetId': sheet_id,
                'dimension': 'ROWS',
                'startIndex': new_row_index,
                'endIndex': new_row_index + 1,
            },
            'inheritFromBefore': True
        }
    }]})

    # Write values into the newly inserted row
    worksheet.update(
        f'A{new_row_index + 1}:E{new_row_index + 1}',
        [row],
        value_input_option='USER_ENTERED'
    )

PROMPT = """You are a nutrition data extractor. The user has sent an image of food, a nutrition label, a menu item, or a meal.

Extract the following and return ONLY valid JSON, no explanation:
{
  "food_name": "...",
  "calories": <number or null>,
  "protein": <grams as number or null>,
  "fat": <grams as number or null>,
  "carbs": <grams as number or null>
}

Rules:
- All macro values should be numbers (not strings), null if unknown
- If there are multiple items, sum them or pick the most prominent one
- Do not include units in the numbers, just the numeric value

Food name formatting — follow this style:
- Include quantity or weight after a comma: "Raspberries, 125g" / "Apple, 1" / "Fanta, 250ml"
- Put variant or descriptor in parentheses before quantity: "Dumplings (pork, fried), 3"
- Put brand or restaurant name at the end after a comma: "Beef Korma, LiteNEasy" / "Honey Chicken, Oriental Hut"
- For restaurant meals with no specific quantity, just name it naturally: "Grilld Mighty Melbourne"
- Use common abbreviations where obvious: g, ml, pc
- More examples from the user's existing food log:
  "Chicken Terriyaki sushi, avocado (2, 250g)"
  "Subway (6 inch italian, schnitzel, mozarella, mayo)"
  "Smiths Chips Original, 170g"
  "Salmon Sandwich, UnaUna (270g)"
  "Cheeseburger, McDonalds, single"
  "Hawaiian pizza, large, Crust"
  "Butter Chicken (frozen), LiteNEasy"
"""


def is_allowed(update: Update) -> bool:
    return update.effective_user.id == ALLOWED_USER_ID


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_allowed(update):
        return
    await update.message.reply_text(
        "Send me a photo of your food, a nutrition label, or a menu item and I'll log it to your sheet."
    )


async def handle_photo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_allowed(update):
        return
    await update.message.reply_text("Got it, extracting macros...")

    photo = update.message.photo[-1]
    file = await context.bot.get_file(photo.file_id)
    buf = BytesIO()
    await file.download_to_memory(buf)
    image_data = base64.standard_b64encode(buf.getvalue()).decode("utf-8")

    caption = update.message.caption
    user_note = f"\nThe user has also provided this note about the food: {caption}" if caption else ""

    try:
        response = claude.messages.create(
            model="claude-opus-4-6",
            max_tokens=256,
            messages=[
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "image",
                            "source": {
                                "type": "base64",
                                "media_type": "image/jpeg",
                                "data": image_data,
                            },
                        },
                        {"type": "text", "text": PROMPT + user_note},
                    ],
                }
            ],
        )

        raw = response.content[0].text.strip()
        # Strip markdown code fences if present
        if raw.startswith("```"):
            raw = raw.split("\n", 1)[1].rsplit("```", 1)[0].strip()

        data = json.loads(raw)
    except Exception as e:
        log.exception("Failed to extract macros")
        await update.message.reply_text(f"Failed to extract macros: {e}")
        return

    row = [
        data.get("food_name", "Unknown"),
        data.get("calories"),
        data.get("protein"),
        data.get("fat"),
        data.get("carbs"),
    ]

    try:
        insert_row_in_table(sheet, row)
    except Exception as e:
        log.exception("Failed to write to sheet")
        await update.message.reply_text(f"Extracted data but failed to write to sheet: {e}")
        return

    name, cals, pro, fat, carbs = row
    await update.message.reply_text(
        f"Logged!\n\n"
        f"*{name}*\n"
        f"Calories: {cals}\n"
        f"Protein: {pro}g\n"
        f"Fat: {fat}g\n"
        f"Carbs: {carbs}g",
        parse_mode="Markdown",
    )


async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_allowed(update):
        return
    await update.message.reply_text("Send me a photo of your food or a nutrition label.")


app = ApplicationBuilder().token(os.environ["TELEGRAM_TOKEN"]).build()
app.add_handler(CommandHandler("start", start))
app.add_handler(MessageHandler(filters.PHOTO, handle_photo))
app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))

log.info("Bot started")
app.run_polling()
