# AstanaQueue — Digital Queue for Concession Transit Passes

**Team:** Yeldos Orazbek & Akzhol Assylbolat (Group SE-2626)  
**Course:** Information and Communication Technologies (Final Project)

AstanaQueue replaces physical lines at concession-pass service centers in Astana with a lightweight digital queuing system for Students (`ST`), Schoolchildren (`SC`), and Seniors (`SR`).

## System Architecture
- **Frontend:** Responsive Single-Page Application (`astanaqueue4.html`) with live 2-second polling, EN/RU/KK localization, `A−`/`A+` font scaling, Web Audio API chime, and `SpeechSynthesis` voice announcements.
- **Backend:** Python Flask REST API (`app4.py`) with role-based PIN protection (`X-Staff-Pin`) and HMAC-SHA256 ticket cancellation tokens.
- **Database:** 3NF-normalized SQLite database (`astanaqueue.sqlite`) with data minimization compliant with Kazakhstan Personal Data Law No. 94-V.

## How to Run Locally
1. Ensure Python 3.9+ is installed.
2. Install required packages:
   ```bash
   pip install -r requirements.txt
