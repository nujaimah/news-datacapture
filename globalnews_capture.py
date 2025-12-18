import asyncio
from datetime import datetime
import re
import os
import json
from playwright.async_api import async_playwright
from googleapiclient.discovery import build as gsheet_build
from googleapiclient.http import MediaFileUpload
from google_auth_oauthlib.flow import InstalledAppFlow
from google.auth.transport.requests import Request
import pickle

SCOPES = ['https://www.googleapis.com/auth/spreadsheets',
          'https://www.googleapis.com/auth/drive']
CLIENT_SECRETS_FILE = 'credentials.json'
TOKEN_PICKLE = 'token.json'
SPREADSHEET_ID = 'NAME' # Enter Google Sheet ID
SHEET_NAME = "NAME" # Enter Google Sheet Tab Name
GLOBALNEWS_CAPTURE_FOLDER_ID = 'NAME' # Enter Google Drive folder ID

def get_oauth_credentials():
    creds = None
    if os.path.exists(TOKEN_PICKLE):
        with open(TOKEN_PICKLE, 'rb') as token:
            creds = pickle.load(token)
    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            creds.refresh(Request())
        else:
            flow = InstalledAppFlow.from_client_secrets_file(CLIENT_SECRETS_FILE, SCOPES)
            creds = flow.run_local_server(port=0)
        with open(TOKEN_PICKLE, 'wb') as token:
            pickle.dump(creds, token)
    return creds

def create_dated_capture_folder(drive_service):
    date_str = datetime.now().strftime("%Y-%m-%d")
    folder_metadata = {
        'name': date_str + " Capture",
        'mimeType': 'application/vnd.google-apps.folder',
        'parents': [GLOBALNEWS_CAPTURE_FOLDER_ID],
    }
    folder = drive_service.files().create(
        body=folder_metadata,
        fields='id'
    ).execute()
    folder_id = folder['id']
    print(f"Created capture folder for {date_str} with ID {folder_id}")
    return folder_id

def normalize_media_url(url: str) -> str:
    if not url:
        return url
    base = url.split("#", 1)[0]
    m = re.match(r"(https://globalnews\.ca/video/embed/(\d+))", base)
    if m:
        return m.group(1)
    return base

async def extract_relevant_article_links(page):
    await page.wait_for_selector("a")
    link_elements = await page.query_selector_all("a")
    article_urls = []

    pattern = re.compile(r"^https?://globalnews\.ca/news/\d+/.+")

    for elem in link_elements:
        href = await elem.get_attribute('href')
        if href:
            full_url = href if href.startswith("http") else f"https://globalnews.ca{href}"
            if pattern.match(full_url) and full_url not in article_urls:
                article_urls.append(full_url)

    print(f"Extracted {len(article_urls)} relevant article URLs")
    return article_urls

async def extract_globalnews_article_info(page):
    video_audio_links = set()

    video_tags = await page.query_selector_all("video")
    for v in video_tags:
        src = await v.get_attribute("src")
        if src:
            video_audio_links.add(normalize_media_url(src))

    audio_tags = await page.query_selector_all("audio")
    for a in audio_tags:
        src = await a.get_attribute("src")
        if src:
            video_audio_links.add(normalize_media_url(src))

    anchors = await page.eval_on_selector_all(
        'a[href^="https://globalnews.ca/player/play/video/"], '
        'a[href^="https://globalnews.ca/player/play/audio/"]',
        'elements => elements.map(el => el.href)'
    )
    for link in anchors:
        video_audio_links.add(normalize_media_url(link))

    iframe_srcs = await page.eval_on_selector_all(
        (
            'iframe.c-video__embed, '
            'iframe[id^="miniplayer_"], '
            'iframe[src*="youtube.com/embed/"], '
            'iframe[src*="youtube-nocookie.com/embed/"]'
        ),
        'nodes => nodes.map(n => n.src)'
    )
    for src in iframe_srcs or []:
        if not src:
            continue
        if "youtube.com/embed/" in src or "youtube-nocookie.com/embed/" in src:
            video_audio_links.add(src.split("?", 1)[0])
        else:
            video_audio_links.add(normalize_media_url(src))

    content = await page.content()
    patterns = [
        r'https://globalnews\.ca/player/play(?:/video)?/[0-9\.]+',
        r'https://globalnews\.ca/player/play/audio/[0-9\.]+',
        r'https://globalnews\.ca/video/embed/[0-9]+[^"\'\s]*',
        r'https://globalnews\.ca/i/phoenix/player/syndicate/\?[^\s"\']+',
        r'https://www\.youtube\.com/embed/[A-Za-z0-9_-]+',
        r'https://www\.youtube-nocookie\.com/embed/[A-Za-z0-9_-]+',
    ]
    for pattern in patterns:
        for match in re.findall(pattern, content):
            if "youtube.com/embed/" in match or "youtube-nocookie.com/embed/" in match:
                video_audio_links.add(match.split("?", 1)[0])
            else:
                video_audio_links.add(normalize_media_url(match))

    jsonld_element = await page.query_selector('script[type="application/ld+json"]')
    jsonld_content = await jsonld_element.text_content() if jsonld_element else None
    if jsonld_content:
        try:
            data = json.loads(jsonld_content)
            if isinstance(data, dict):
                videos = data.get("video", [])
                if isinstance(videos, dict):
                    videos = [videos]
                for v in videos:
                    for key in ["embedUrl", "contentUrl"]:
                        url = v.get(key)
                        if url:
                            if "youtube.com/embed/" in url or "youtube-nocookie.com/embed/" in url:
                                video_audio_links.add(url.split("?", 1)[0])
                            else:
                                video_audio_links.add(normalize_media_url(url))

                audios = data.get("audio", [])
                if isinstance(audios, dict):
                    audios = [audios]
                for a in audios:
                    for key in ["embedUrl", "contentUrl"]:
                        url = a.get(key)
                        if url:
                            video_audio_links.add(normalize_media_url(url))
        except json.JSONDecodeError:
            pass

    additional_authors = ""
    em_elements = await page.query_selector_all("article p em")
    for em in em_elements:
        text = (await em.text_content()).strip()
        if text.lower().startswith("with files by") or text.lower().startswith("with files from"):
            additional_authors = text.strip("—").strip()
            break

    return sorted(video_audio_links), additional_authors

async def check_ai_mention(page):
    keywords = ["ChatGPT", "automated", "robot", "AI tools", "data team", "OpenAI", "Otter.ai",
                "AI-Based", "artificial intelligence", "machine learning", "AI model",
                "AI technology", "AI-generated", "AI-assisted"]
    try:
        article_element = await page.query_selector("article")
        if not article_element:
            return "False"
        paragraphs = await article_element.query_selector_all("p")
        text_content = " ".join([await p.inner_text() for p in paragraphs])
    except Exception:
        return "False"

    text_lower = text_content.lower()
    for kw in keywords:
        if kw.lower() in text_lower:
            return f"True - {kw}"
    return "False"

async def extract_author_contacts(context, profile_urls):
    contacts = set()
    seen_profiles = set()

    for purl in profile_urls or []:
        if not purl or purl in seen_profiles:
            continue
        seen_profiles.add(purl)

        page = await context.new_page()
        try:
            await page.goto(purl, wait_until="domcontentloaded", timeout=60000)
            await page.wait_for_timeout(1000)

            try:
                text = await page.inner_text("body")
            except Exception:
                text = ""

            for m in re.findall(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}", text):
                contacts.add(f"mailto:{m}")

            for m in re.findall(r"@([A-Za-z0-9_]{2,})", text):
                handle = m.lower()
                if handle in {"am640", "globalnews"}:
                    continue
                contacts.add(f"https://twitter.com/{handle}")

            links = await page.query_selector_all("a[href]")
            for a in links:
                href = await a.get_attribute("href")
                if not href:
                    continue

                if ("twitter.com/am640" in href or "x.com/am640" in href or
                        "twitter.com/globalnews" in href or "x.com/globalnews" in href):
                    continue
                if "linkedin.com/company/global-television" in href:
                    continue

                if "twitter.com/intent/tweet" in href:
                    continue
                if href.startswith("mailto:?"):
                    continue

                if href.startswith("mailto:") and "@" in href:
                    contacts.add(href)
                    continue

                m_tw = re.match(r"^https?://(twitter|x)\.com/([^/?#]+)$", href)
                if m_tw:
                    handle = m_tw.group(2).lower()
                    if handle in {"am640", "globalnews"}:
                        continue
                    contacts.add(href)
                    continue

                if re.match(r"^https?://(www\.)?linkedin\.com/in/[^/?#]+", href):
                    contacts.add(href)
                    continue

        except Exception:
            pass
        finally:
            await page.close()

    return "\n".join(sorted(contacts))

async def save_pdf_with_metadata(playwright_page, url, drive_service, folder_id):
    await playwright_page.goto(url, wait_until="domcontentloaded", timeout=60000)
    await playwright_page.wait_for_timeout(2000)

    title_element = await playwright_page.query_selector("h1")
    title = await title_element.inner_text() if title_element else "No title found"

    authors = []
    author_profile_links = []
    try:
        author_elements = await playwright_page.query_selector_all(
            ".c-byline__attribution span a.c-byline__name.c-byline__link"
        )
        for author_elem in author_elements:
            text = await author_elem.text_content()
            href = await author_elem.get_attribute("href")
            if text:
                authors.append(text.strip())
            if href:
                full = href if href.startswith("http") else f"https://globalnews.ca{href}"
                author_profile_links.append(full)
    except Exception:
        pass

    if not authors:
        try:
            author_text = await playwright_page.eval_on_selector(
                "#article-byline .c-byline__attribution span:first-child",
                "el => el.textContent.trim().replace(/^By\\s+/i, '')"
            )
            if author_text:
                authors = [author_text]
        except Exception:
            authors = []

    affiliation = ""
    try:
        affiliation = await playwright_page.eval_on_selector(
            ".c-byline__source.c-byline__source--hasName, .c-byline__source.c-byline__source--noName",
            "el => el.textContent.trim()"
        )
    except Exception:
        affiliation = ""

    authors_str = ", ".join(authors) if authors else "No author found"
    affiliation_str = affiliation if affiliation else "No affiliation found"

    date_posted = "No date found"
    publish_date = ""
    updated_date = ""

    try:
        publish_date = await playwright_page.eval_on_selector(
            ".c-byline__date--pubDate span",
            "el => el.textContent.replace('Posted ', '').trim()"
        )
    except Exception:
        pass

    try:
        updated_date = await playwright_page.eval_on_selector(
            ".c-byline__date--ModDate span, .c-byline__date--modDate span",
            "el => el.textContent.replace('Updated ', '').trim()"
        )
    except Exception:
        pass

    if publish_date and updated_date:
        date_posted = f"{publish_date} (Updated: {updated_date})"
    elif publish_date:
        date_posted = publish_date
    elif updated_date:
        date_posted = f"Updated: {updated_date}"

    date_str = datetime.now().strftime("%Y-%m-%d")
    safe_title = "".join(c for c in title if c.isalnum() or c in (" ", "-")).replace(" ", "_")[:60]
    pdf_file = f"globalnews_story_{safe_title}_{date_str}.pdf"

    print(f"Title: {title}")
    print(f"Authors: {authors_str}")
    print(f"Affiliation: {affiliation_str}")
    print(f"Date posted: {date_posted}")
    print(f"Saving PDF: {pdf_file}")

    await playwright_page.pdf(
        path=pdf_file,
        format="A4",
        print_background=True,
        margin={"top": "10mm", "bottom": "10mm", "left": "10mm", "right": "10mm"}
    )

    file_metadata = {'name': os.path.basename(pdf_file), 'parents': [folder_id]}
    media = MediaFileUpload(pdf_file, mimetype='application/pdf', resumable=True)
    file = drive_service.files().create(
        body=file_metadata, media_body=media, fields='id'
    ).execute()
    print(f"Uploaded {pdf_file} to Google Drive with file ID {file['id']}")

    return (title, authors_str, affiliation_str, url, date_posted, author_profile_links)

def ensure_header_row(service):
    sheet = service.spreadsheets()
    header = [[
        "Title",
        "Author",
        "Social/Email",
        "Affiliation",
        "Link",
        "Date Posted/Last Updated",
        "Additional Affiliations",
        "Video/Audio Links",
        "AI Mention?"
    ]]
    sheet.values().update(
        spreadsheetId=SPREADSHEET_ID,
        range=f"{SHEET_NAME}!A1:I1",
        valueInputOption="RAW",
        body={'values': header}
    ).execute()

def append_to_google_sheet(data_rows, service):
    sheet = service.spreadsheets()
    body = {'values': data_rows}
    result = sheet.values().append(
        spreadsheetId=SPREADSHEET_ID,
        range=f"{SHEET_NAME}!A:I",
        valueInputOption="RAW",
        insertDataOption="INSERT_ROWS",
        body=body
    ).execute()
    print(f"{result.get('updates').get('updatedRows')} rows appended to Google Sheet")

async def main():
    creds = get_oauth_credentials()
    drive_service = gsheet_build('drive', 'v3', credentials=creds)
    sheet_service = gsheet_build('sheets', 'v4', credentials=creds)

    capture_folder_id = create_dated_capture_folder(drive_service)

    homepage_url = "https://globalnews.ca"
    date_str = datetime.now().strftime("%Y-%m-%d")
    homepage_pdf = f"globalnews_homepage_{date_str}.pdf"

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        context = await browser.new_context(
            viewport={"width": 1600, "height": 4000},
            ignore_https_errors=True
        )
        page = await context.new_page()

        await page.goto(homepage_url, wait_until="domcontentloaded", timeout=120000)
        await page.wait_for_timeout(2000)

        article_urls = await extract_relevant_article_links(page)
        print(f"Filtered {len(article_urls)} article URLs after extraction.")
        for url in article_urls:
            print(url)

        await page.pdf(
            path=homepage_pdf,
            format="A4",
            print_background=True,
            margin={"top": "10mm", "bottom": "10mm", "left": "10mm", "right": "10mm"}
        )
        print(f"Homepage PDF saved as {homepage_pdf}")
        file_metadata = {'name': os.path.basename(homepage_pdf), 'parents': [capture_folder_id]}
        media = MediaFileUpload(homepage_pdf, mimetype='application/pdf', resumable=True)
        drive_service.files().create(
            body=file_metadata, media_body=media, fields='id'
        ).execute()

        metadata_rows = []
        for link in article_urls:
            article_page = await context.new_page()
            meta = await save_pdf_with_metadata(article_page, link, drive_service, capture_folder_id)
            video_audio_links, additional_author_info = await extract_globalnews_article_info(article_page)
            ai_mention = await check_ai_mention(article_page)
            additional_affiliations = ", ".join(x for x in [additional_author_info] if x)

            social_email = await extract_author_contacts(context, meta[5])

            row = (
                meta[0],  # Title
                meta[1],  # Author
                social_email,  # Social/Email
                meta[2],  # Affiliation
                meta[3],  # Link
                meta[4],  # Date Posted/Last Updated
                additional_affiliations,  # Additional Affiliations
                "\n".join(video_audio_links) if video_audio_links else "",  # Video/Audio Flag
                ai_mention  # AI Mention?
            )
            metadata_rows.append(row)
            await article_page.close()

        await browser.close()

    ensure_header_row(sheet_service)
    append_to_google_sheet(metadata_rows, sheet_service)

if __name__ == "__main__":
    asyncio.run(main())
