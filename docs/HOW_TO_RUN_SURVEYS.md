# Feedora Survey Platform — Self-Service Researcher Playbook

Welcome to the Feedora Research Survey platform. This guide explains how to create, share, and collect responses for your research study without needing any programming or technical assistance.

---

## 1. How to Share Your Survey with Participants

Every survey you create has a dedicated **Survey sharing details** tab in your dashboard:

1. Log into your Feedora account at `/accounts/login/creator/`.
2. Navigate to **My surveys** and select your survey to open its dashboard.
3. In the left navigation menu, click **Survey sharing details**.

You will see:
* **Direct Web Link**: Send this link directly via email, WhatsApp, or chat.
* **Access Code** (e.g. `HM89X2`): A clean 6-character code participants can type.
* **Scan QR Code**: A high-resolution scannable 2D barcode.

---

## 2. Collecting Data in Classrooms or Lecture Halls

### Option A: Projecting the QR Code on Screen
* On the **Survey sharing details** screen, the QR code is generated live.
* Project this page onto the lecture screen.
* Students simply open their regular phone camera (iPhone or Android) and point it at the screen. The survey opens in their mobile browser instantly.

### Option B: Printing Physical Survey Handouts / Placards
* Click the **"🖨️ Print Placard"** button or press `Ctrl + P`.
* The page automatically reformats into a clean, distraction-free printable handout containing your survey title, access code, and large scannable QR code.
* Alternatively, click **"↓ Download QR (PNG)"** to save the QR code image and paste it into lecture slides or research handouts.

---

## 3. Web vs. Mobile App: How Do Respondents Take the Survey?

| Participant Device | What the Participant Does | How Data is Recorded |
| :--- | :--- | :--- |
| **Any Phone / Laptop (Web)** | Scans the QR code with phone camera or clicks the link. | Answers save directly into your Feedora database in real time. |
| **Mobile Companion App (Android/Expo)** | Opens the Feedora Mobile app, types your 6-character code (e.g. `HM89X2`), and completes the survey. | Answers submit via the secure REST API directly into your database. |

---

## 4. Viewing Results & Exporting to SPSS / R / Excel

1. In your survey dashboard, click the **Dashboard** tab.
2. View real-time:
   * **Overall Engagement Score** & HMSAM Construct Breakdown (PEOU, PU, Immersion, Control, Curiosity).
   * **Question-by-Question charts** (bar charts and rating breakdowns).
   * **Individual Respondent answers**.
3. To perform statistical analysis (Cronbach's alpha, regression, structural equation modeling):
   * Click the **"↓ Export CSV"** button in the top-right corner.
   * Open the `.csv` file directly in Excel, SPSS, R, or Python.

---

## 5. Troubleshooting & FAQ

* **What if lecture hall Wi-Fi blocks student phones from reaching my local laptop?**
  * If testing locally on your laptop, ensure both your laptop and participants are on the same Wi-Fi network.
  * For completely remote or campus-wide data collection, host your Django server on a standard web domain (e.g. Render, Railway, or university server).
* **Can participants submit answers multiple times?**
  * Feedora tracks respondent session identifiers and timestamps to prevent duplicate submissions.
