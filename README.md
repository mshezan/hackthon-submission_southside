# FinTrack — Personal Finance Dashboard

> A modern, AI-powered personal finance and retirement planning platform built for the Indian fintech context.

## Video Demonstration
https://drive.google.com/file/d/1byFdtJ3fFMj0AL6yGy-c6UeHpgZc66aF/view?usp=sharing
---

##  Problem Statement

Managing personal finances remains a challenge for most working professionals in India:

- **Opaque spending patterns** — most users have no clear view of where their salary goes month-over-month
- **Fragmented banking** — income flows across multiple banks, UPI apps, and cards, making aggregation impossible without technical effort
- **Zero retirement clarity** — long-term planning is deferred because the math is never visible, actionable, or personal

FinTrack solves all three problems in a single dashboard.

---

##  Solution

FinTrack is a **personal finance aggregation dashboard** that:

1. **Aggregates accounts** — links multiple bank accounts via PAN-based identity (AA / mock bank API)
2. **Analyzes transactions** — automatically categorizes and visualizes UPI, bank transfer, and card spending
3. **Delivers insights** — shows monthly income vs. expense trends, top spending categories, and savings rate
4. **Plans retirement** — projects retirement corpus, identifies optimal savings levers, and visualizes the impact of small changes on your retirement age

---
##  Features

| Feature | Description |
|---------|-------------|
|  **Account Linking (PAN-based)** | Enter your PAN, fetch all associated bank accounts, and link them in 3 steps |
|  **Multi-bank Aggregation** | Aggregate accounts from HDFC, SBI, ICICI, and more simultaneously |
|  **Transaction Analysis** | Monthly income vs. expense bar chart with category breakdowns |
|  **UPI-first Insights** | Transactions classified by UPI, Bank Transfer, or Card — aligned with Indian payment patterns |
|  **Retirement Planning** | AI-driven retirement corpus projection based on real transaction data |
|  **Interactive Timeline** | Visualize how action cards (save more, cut expenses) shift your retirement age |
|  **Secure Profile** | Masked account numbers, PAN-based identity, per-user data isolation |

---

##  Tech Stack

| Layer | Technology |
|-------|-----------|
| Backend | Python 3 + Flask |
| Database | SQLite (via SQLAlchemy ORM) |
| Styling | Tailwind CSS (CDN) + custom design tokens |
| Charts | Chart.js (income vs. expense bar chart) |
| Icons | Lucide Icons |
| Mock Bank API | Custom Flask server (`bank_server.py`) |
| Auth | Flask-Login with bcrypt password hashing |

---

##  Setup Instructions

### Prerequisites

- Python 3.9+
- pip

### Installation

```bash
# 1. Clone the repository
git clone https://github.com/your-username/fintrack.git
cd fintrack

# 2. Create and activate a virtual environment
python -m venv venv
# On Windows:
venv\Scripts\activate
# On Mac/Linux:
source venv/bin/activate

# 3. Install dependencies
pip install -r requirements.txt

# 4. Set up the database
python init_db.py

# 5. Start the mock bank API server (required for account linking)
python bank_server.py &

# 6. Start the main application
python app.py
```

Open `http://localhost:5000` in your browser.

---

## 🎬 Demo Instructions

Follow this flow to experience all features:

1. **Login** — Use demo credentials: `demo@fintrack.com` / `demo123`
2. **Navigate to Accounts** — Click the pie-chart icon in the sidebar
3. **Enter PAN** — Type `ABCDE1234F` (or any 5-32 alphanumeric string) and click "Fetch accounts"
4. **Link an account** — Select a bank account from the list, give it a name, and click "Connect Account"
5. **Back to Dashboard** — See your balance card, monthly activity chart, and recent transactions
6. **Explore Transactions** — Filter by month, search by description, assign categories
7. **Open Retirement** — See your projected retirement corpus, snapshot metrics, and interactive action cards — click an action card to see how it shifts your retirement age on the timeline

---

##  APIs Used

| API | Purpose |
|-----|---------|
| **Mock Bank Server** (`bank_server.py`) | Simulates a real AA (Account Aggregator) bank gateway — returns accounts and transactions for a given identity ID |
| **`/api/bank/discover`** | Identity-based account discovery — fetches all linked banks for a PAN |
| **`/api/bank/connect`** | Links a selected account to the user's FinTrack profile |
| **`/api/accounts/sync`** | Fetches and stores latest transactions for linked accounts |
| **`/api/monthly-activity`** | Returns monthly income/expense data for Chart.js |
| **`/api/retirement/analysis`** | Runs retirement corpus projections based on real transaction history |
| **`/api/transactions/<id>/categorize`** | Updates transaction category in real time |

---

##  Project Structure

```
fintech/
├── app.py                  # Main Flask application + all routes
├── auth.py                 # Authentication blueprint (login/register/logout)
├── bank_api.py             # Bank API client (calls bank_server)
├── bank_server.py          # Mock bank API server (FastAPI-compatible Flask server)
├── models.py               # SQLAlchemy models (User, LinkedAccount, Transaction, …)
├── retirement_service.py   # Retirement analysis & projection engine
├── services.py             # Shared services (categorization, account helpers)
├── config.py               # App configuration
├── requirements.txt        # Python dependencies
├── static/
│   ├── css/fintech-theme.css   # Custom design tokens & utilities
│   └── js/
│       ├── dashboard.js        # Chart rendering + account switcher
│       ├── accounts.js         # Bank connect flow + step progress
│       └── transactions.js     # Month filter + category + search
└── templates/
    ├── base.html               # Sidebar, header, profile dropdown
    ├── dashboard.html          # Balance card, activity chart, recent transactions
    ├── accounts.html           # PAN flow, linked accounts
    ├── transactions.html       # Transaction table with search and filter
    ├── retirement.html         # Retirement timeline + action cards
    ├── login.html
    └── register.html
```

---

##  Running Tests

```bash
# Test the bank API server is reachable
curl http://localhost:5001/health

# Test the main app
curl http://localhost:5000/
```

---

##  Notes

- The **mock bank server** (`bank_server.py`) must be running alongside `app.py` for account linking to work
- All data is stored locally in `instance/fintrack.db` (SQLite)
- For production, replace SQLite with PostgreSQL and the mock bank server with a real AA gateway

---

© 2026 FinTrack — Built with Flask, Tailwind CSS, and Chart.js
