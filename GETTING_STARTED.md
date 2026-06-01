# Ditat Verify — Getting Started

A quick guide. Follow the steps in order. You only do Setup once.

---

## Before you start

You need two things from your administrator:

1. **Server address** — looks like `https://something.up.railway.app`
2. **Access key** — a long secret string

Keep them handy for Step 5.

---

## One-time setup

### 1. Install Python (if you don't have it)

- Go to **https://www.python.org/downloads/**
- Click the big **Download Python** button, run the installer.
- ✅ On the first screen, check the box **"Add Python to PATH"**, then click Install.

*(Already have Python? Skip this.)*

### 2. Open Claude Code

Open the **Claude Code** app (or the Claude extension in VS Code).

### 3. Install the plugin

In the Claude chat box, type these two lines, one at a time, and press Enter:

```
/plugin marketplace add boxa-devops/ditat-verify-plugin
/plugin install ditat-verify@ditat-tools
```

Wait a few seconds after each.

### 4. Start setup

Type this and press Enter:

```
set up ditat
```

Claude will ask you a few simple questions:

- **Where to keep it?** → choose **Desktop** (recommended).
- **What to name it?** → just press accept for **ditat-verify**.

Claude creates the folder for you.

### 5. Enter your server address and key

Claude will ask for the **server address** and **access key** from your admin.

- Paste them in the chat when asked, **or**
- Claude opens a file called `.env` — type the two values in, save, and tell Claude **"done"**.

### 6. Check it works

Claude runs a quick test. When you see **OK**, you're ready. 🎉

---

## Everyday use

Whenever you want to check shipments, open Claude Code and type one of these:

```
verify last week
```
```
verify last month
```

Claude does the rest. When it finishes, it gives you a **Word document** with the results.

You'll find that document in the **`reports`** folder on your Desktop, inside the **`ditat-verify`** folder.

---

## If something looks wrong

- **"OK: false" / can't connect** → double-check the server address and key with your admin, then type `set up ditat` again.
- **"python is not recognized"** → Python isn't installed or the PATH box wasn't checked. Re-install Python (Step 1) and tick **"Add Python to PATH"**.
- **No shipments found** → there may be nothing delivered in that time window. Try `verify last month`.

Still stuck? Send your admin a screenshot of the message Claude showed.

---

That's it. Setup once, then just type **"verify last week"** whenever you need it.
