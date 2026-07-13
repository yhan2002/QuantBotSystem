# QuantBot

This repository tracks two code lines:

- `QuantBotSystem/` — deployment version
- `QuantBotSystem_Beta/` — beta/update version

## Weekly update flow

The **QuantBot: publish weekly update** VS Code task safely stages source changes, creates one weekly commit, and pushes it when an `origin` remote is configured. Local credentials, runtime data, and generated files are excluded.

The scheduled Codex task then summarizes that week's deployment and beta changes in the Notion database **QuantBot 주간 업데이트** for the meeting.

To connect GitHub after creating an empty private repository:

```powershell
git remote add origin https://github.com/<your-account>/QuantBot.git
git push -u origin main
```

