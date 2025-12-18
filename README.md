# news-datacapture

This script uses the *Playwright* and *Newspaper* Python libraries to capture PDF and PNG screenshots of the CBC, Global News, and La Presse homepage and individual articles, as well as extract data of individual articles including: title, author (and author social media/email), link, date posted, video/audio links, and mention of AI

## Features

- Captures **PDF and PNG snapshots** of homepages and individual articles
- Extracts comprehensive **article metadata**:
  - Title, author(s), social media/email contacts
  - Article URL and publication dates
  - Video/audio links (native players + YouTube embeds)
  - AI-related content detection
- Automatically organizes files in **dated Google Drive folders**
- Appends structured data to **Google Sheets**
- Handles dynamic content loading and infinite scroll

---

## Requirements

**Python Version:** 3.9 or newer

**Ability to run Chromium in headless mode (Playwright will install it)**

Install required packages and browser dependencies:

```Shell
# install playwright
pip install playwright
# install chromium browser
playwright install chromium
# install all Google packages
pip install google-api-python-client google-auth google-auth-oauthlib google-auth-httplib2
```

### Google API Setup
1. **Create Google Cloud project** and enable:
   - [Google Drive API](https://console.cloud.google.com/apis/library/drive.googleapis.com)
   - [Google Sheets API](https://console.cloud.google.com/apis/library/sheets.googleapis.com)
2. **Create OAuth 2.0 credentials** → Download `credentials.json`
3. **Configure script constants in the Python files**:
   - `SPREADSHEET_ID`: Your target Google Sheet ID
   - Drive folder IDs (one per news site)
4. Place `credentials.json` in the project directory

---

## Running the Scripts (CBC, Global News, La Presse)

1. Save the script as `cbc_capture.py` and run:

```Shell
python cbc_capture.py
```

The script will open Chromium, load the CBC News homepage, and save a PDF and PNG screenshot of the homepage in a new folder in Google Drive. Then it will retrieve all individual article URLs and open separate Chromium tabs to capture individual PDF screenshots of each article, which will be stored in the same folder. Lastly, the metadata of each article will be extracted and pasted into Google Sheets. 

2. Save the script as `globalnews_capture.py` and run:

```Shell
python globalnews_capture.py
```

The script will run in the background (in headless mode) and save all screenshots and data on Google Drive and Sheets.

3. Save the script as `lapresse_capture.py` and run:

```Shell
python lapresse_capture.py
```

The script will run in the background (in headless mode) and save all screenshots and data on Google Drive and Sheets.




