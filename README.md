# Canvas to Storage Scraper & Sync

This script automates the process of downloading all files from your Canvas LMS courses and saving them either to a local directory on your computer or uploading them to a specified folder in your Google Drive, maintaining the course structure. It's designed to be run periodically to keep your storage in sync with Canvas.

## Features

- Connects to the Canvas LMS API to fetch your courses and files.
- **Choice of storage**: Save files locally or upload to Google Drive.
- **Remembers last selection**: Automatically saves your course selection and offers to reuse it next time.
- Creates a root folder/directory for organization.
- Creates subfolders for each of your Canvas courses.
- **Comprehensive page discovery**: Finds pages from both the Canvas Pages API and pages within modules.
- **Consolidated page bundle**: Generates a single "All Pages.pdf" containing all course pages with table of contents.
- **Assignment PDFs**: Converts assignments to PDF format with descriptions and details.
- For each Page in a course, creates a dedicated subfolder named after the Page.
- Saves/uploads the Page's content and any files linked from that Page into the Page's subfolder.
- **Smart change detection**: Only syncs when content has been updated or new content is added.
- Cleans up temporary local files after processing.

## Folder Structure

The script organizes your synced files in the following structure (works for both local storage and Google Drive):

```
Canvas Sync (Root Folder/Directory)
├── Course Name 1
│   ├── Assignments
│   │   ├── Assignment 1.pdf
│   │   └── Assignment 2.pdf
│   ├── Pages
│   │   └── All Pages.pdf
│   ├── Direct Module Files...
│   ├── Page Title 1
│   │   ├── Page Title 1.pdf
│   │   └── Linked Files from Page...
│   └── Page Title 2
│       ├── Page Title 2.pdf
│       └── Linked Files from Page...
└── Course Name 2
    └── ...
```

- **Course Folders**: One folder per Canvas course.
- **Assignments Folder**: Contains PDF versions of all course assignments.
- **Pages Folder**: Contains "All Pages.pdf" - a consolidated bundle of all course pages with a table of contents.
- **Direct Module Files**: Files directly attached to course modules are saved here.
- **Page Subfolders**: Each course page also gets its own subfolder containing the page's PDF and any linked files.

## Note on How Files Are Found

Many institutions disable the main "Files" tab in Canvas courses. This prevents the API from directly listing all files.

This script works around that limitation by searching for files that are linked within Course Modules and Pages. This will find the vast majority of course materials, including files directly attached to modules and those embedded or linked within course pages.

**Page Discovery**: The script uses a comprehensive approach to discover all course pages by:

1. Querying the Canvas Pages API endpoint directly
2. Scanning all course modules to find pages that may only exist within modules
3. Combining both sources to ensure no pages are missed

This dual-source approach ensures that new pages added to modules are detected and included in the sync, even if they don't appear in the main Pages listing.

**Note**: If a file is uploaded to the course but not linked in any module, page, or assignment, this script will not find it.

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

## Sync Behavior and Change Detection

The script is designed to be efficient and only sync content when changes are detected:

- **Assignments**: Regenerated when Canvas reports a newer `updated_at` timestamp
- **Pages**: The "All Pages.pdf" is rebuilt when any page in the course has been updated or new pages are added
- **Files**: Only downloaded if file size differs or modification time is newer
- **Summary Report**: At the end of each sync, you'll see a detailed report showing which files were updated or created

This smart change detection means you can run the script frequently without wasting bandwidth or storage operations on unchanged content.

## Troubleshooting

### Pages Not Updating

If you notice that new pages aren't appearing in the "All Pages.pdf" bundle:

- Make sure the pages are published in Canvas
- Check that the pages are either in the main Pages section or linked within course modules
- The script now discovers pages from both sources automatically (as of the latest update)

### Missing Files

If some files aren't syncing:

- Verify the files are linked in a module, page, or assignment
- Files that exist in Canvas but aren't linked anywhere won't be discovered
- Check that you have proper API permissions in Canvas

### Google Drive Authorization Issues

If you encounter authorization errors:

- Delete `token.json` and re-run the script to re-authenticate
- Verify `credentials.json` is in the same folder as `main.py`
- Make sure the Google Drive API is enabled in your Google Cloud project

## Building a Standalone Executable (PyInstaller `.spec`)

You can package the script as a distributable executable using PyInstaller and the provided `CanvasSync.spec` file. This is useful if you want to run the sync on a machine without installing Python dependencies each time, or schedule it via Task Scheduler.

### 1. Install Dependencies

```powershell
pip install -r requirements.txt
pip install pyinstaller
```

### 2. Build with the Spec File

The project includes a PyInstaller spec file: `CanvasSync.spec`. Build using:

```powershell
pyinstaller CanvasSync.spec
```

After a successful build you'll see:

- `dist/CanvasSync/` – Folder containing the executable and its needed files
- `build/` – Intermediate build artifacts (can be deleted after confirming the exe works)
- `CanvasSync.spec` – The spec file you invoked (editable to customize build)

Run the generated executable from `dist/CanvasSync/` (it may be named `CanvasSync.exe`). The working directory should still contain your `config.ini`, `credentials.json` (if using Google Drive), and any generated `token.json`.

### 3. Cleaning Previous Builds

Before rebuilding, you can remove old artifacts:

```powershell
Remove-Item -Recurse -Force .\dist, .\build
```

### 4. Customizing the Build

Edit `CanvasSync.spec` if you need to:

- Add data files: include `config.ini.example` so it's packaged for users new to the app
- Add hidden imports: if a module is missing at runtime (e.g., some dynamic imports)
- Toggle console visibility: set `console=False` for a silent windowed build
- One-file option: convert the folder build into a single EXE using `--onefile` (may increase startup time)

Example alternative one-file build (ignores the spec and uses defaults):

```powershell
pyinstaller --onefile --name CanvasSync main.py
```

If you need to regenerate the spec (e.g., after major changes):

```powershell
pyinstaller main.py --name CanvasSync --specpath . --onefile --dry-run
```

Then adjust the generated spec for datas / options and rebuild with `pyinstaller CanvasSync.spec`.

### 5. Common Build Issues

- Missing module at runtime: add it to `hiddenimports` inside the spec
- Large executable size: prefer folder build; strip/exclude optional libs not used
- Google OAuth not launching: ensure browser access; run from a writable directory so `token.json` can be created

### 6. Distributing

Share only the `dist/CanvasSync/` folder (or the single EXE if you used `--onefile`). Include a sample `config.ini.example` so end users can configure their environment. Users still need their own Canvas API key and (optionally) `credentials.json` for Google Drive.

---
