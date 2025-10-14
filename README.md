# Canvas to Storage Scraper & Sync

This script automates the process of downloading all files from your Canvas LMS courses and saving them either to a local directory on your computer or uploading them to a specified folder in your Google Drive, maintaining the course structure. It's designed to be run periodically to keep your storage in sync with Canvas.

## Features

- Connects to the Canvas LMS API to fetch your courses and files.
- **Choice of storage**: Save files locally or upload to Google Drive.
- **Remembers last selection**: Automatically saves your course selection and offers to reuse it next time.
- Creates a root folder/directory for organization.
- Creates subfolders for each of your Canvas courses.
- For each Page in a course, creates a dedicated subfolder named after the Page.
- Saves/uploads the Page's HTML content and any files linked from that Page into the Page's subfolder.
- Checks for existing files to avoid duplicates, only syncing new files.
- Cleans up temporary local files after processing.

## Folder Structure

The script organizes your synced files in the following structure (works for both local storage and Google Drive):

```
Canvas Sync (Root Folder/Directory)
├── Course Name 1
│   ├── Direct Module Files...
│   ├── Page Title 1
│   │   ├── Page Title 1.html
│   │   └── Linked Files from Page...
│   └── Page Title 2
│       ├── Page Title 2.html
│       └── Linked Files from Page...
└── Course Name 2
    └── ...
```

- **Course Folders**: One folder per Canvas course.
- **Direct Module Files**: Files directly attached to course modules are saved here.
- **Page Subfolders**: Each course page gets its own subfolder containing the page's HTML and any linked files.

## Note on How Files Are Found

Many institutions disable the main "Files" tab in Canvas courses. This prevents the API from directly listing all files.

This script works around that limitation by searching for files that are linked within Course Modules and Pages. This will find the vast majority of course materials, including files directly attached to modules and those embedded or linked within course pages. However, please be aware: If a file is uploaded to the course but not linked in any module or page, this script will not find it.

## Setup Instructions

This script requires some initial setup to grant it access to your Canvas and Google Drive accounts. Follow these steps carefully.

### Step 1: Clone or Download Files

Download all the files from this project (`main.py`, `requirements.txt`, `config.ini.example`, and this README) into a new folder on your computer.

### Step 2: Install Python Libraries

Make sure you have Python 3 installed.

Open your terminal or command prompt.

Navigate to the folder where you downloaded the files.

Install the required libraries by running:

```sh
pip install -r requirements.txt
```

Or, manually install:

```sh
pip install requests google-api-python-client google-auth-httplib2 google-auth-oauthlib
```

### Step 3: Configure Canvas API Access

1. Log in to your Canvas instance (e.g., https://your-school.instructure.com).
2. Click on Account in the left-hand navigation bar, then Settings.
3. Scroll down to the Approved Integrations section and click + New Access Token.
4. Give the token a Purpose (e.g., "Storage Sync Script") and click Generate Token.
5. Immediately copy the generated token. You will not be able to see it again.
6. Open the `config.ini.example` file and rename it to `config.ini`.
7. In `config.ini`, paste the token as the value for `API_KEY`.
8. Set the `API_URL` to your Canvas instance's URL (e.g., https://your-school.instructure.com).

### Step 4: Choose Storage Method

In your `config.ini` file, set the `STORAGE_TYPE` to either:

- `local` - Save files to a local directory on your computer
- `google_drive` - Upload files to Google Drive

For local storage:

- Set `LOCAL_ROOT_DIR` to the directory where you want to save files (e.g., `./canvas_sync`)

For Google Drive storage:

- Set `ROOT_FOLDER_NAME` to the name of the root folder in Google Drive
- Follow the Google Drive API setup steps below

### Step 5: Configure Google Drive API Access (Only if using Google Drive)

If you chose `google_drive` as your storage type, follow these additional steps:

1. Go to the Google Cloud Console and create a new project (or select an existing one).
2. Go to APIs & Services -> Library.
3. Search for "Google Drive API" and enable it for your project.
4. Go to APIs & Services -> Credentials.
5. Click + CREATE CREDENTIALS and choose OAuth client ID.
6. If prompted, configure the OAuth consent screen.
   - Choose External for User Type.
   - Fill in the required fields (App name, User support email, Developer contact information). You can use "Canvas Sync" for the app name. Click Save and Continue through the Scopes and Test Users sections. Finally, click Back to Dashboard.
7. Now, create the OAuth client ID again.
   - Select Desktop app for the Application type.
   - Give it a name (e.g., "Canvas Scraper Credentials").
   - Click Create.
8. A window will pop up with your credentials. Click DOWNLOAD JSON.
9. Rename the downloaded file to `credentials.json` and move it into the same folder as the `main.py` script.

### Step 6: Run the Script

You are now ready to run the scraper.

Open your terminal or command prompt and navigate to the project folder.

Run the script:

```sh
python main.py
```

**First-Time Google Drive Authorization:** If you chose Google Drive storage, the first time you run it, a new tab will open in your web browser asking you to authorize access to your Google Account. Follow the prompts to grant permission. After you approve, a `token.json` file will be created in your project folder. This stores your authorization so you won't have to log in every time.

The script will now start fetching your courses and syncing new files to your chosen storage location.

Sit back and let it run! You can run this script as often as you like to check for new files.

### Course Selection Memory

The script remembers your last course selection and offers it as an option for future runs:

- When you select courses, the script automatically saves your choice to `config.ini`.
- On subsequent runs, courses from your last selection are marked with "(last selected)".
- You can quickly reuse your last selection by typing "last" when prompted for course selection.
- If your last selected courses are no longer available, you'll be prompted to select manually.

This feature makes it convenient to sync the same set of courses repeatedly without re-selecting them each time.

## Performance tuning

For large courses, you can speed up syncs by tweaking the optional [PERFORMANCE] section in `config.ini`:

- REQUEST_TIMEOUT: HTTP timeout in seconds (default 20)
- MAX_RETRIES: Retries on transient errors (429/5xx) with exponential backoff (default 3)
- BACKOFF_FACTOR: Backoff multiplier between retries (default 0.5)
- CANVAS_PER_PAGE: Canvas API page size to cut down pagination (default 100)
- HTTP_POOL_MAXSIZE: HTTP connection pool size for Canvas requests (default 20)
- DRIVE_CHUNK_SIZE_MB: Google Drive resumable upload chunk size in MB (default 8)

The script also reuses a single connection-pooled HTTP session and only regenerates PDFs or re-downloads files when Canvas reports a newer update time or file size change. This avoids unnecessary work on repeated runs.
