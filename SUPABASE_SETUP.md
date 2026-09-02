# Optional: Supabase accounts for RadioBeat

The monitor works with **no account at all** — just run the server and click
"Continue without an account". Everything is stored locally in `radiobeat.db`.

Add Supabase only if you want sign-in and cloud-synced session summaries, so a
family member can check on someone from another device.

---

## 1. Create the project

1. Go to <https://supabase.com>, sign up, and create a new project.
2. Open **Settings → API** and copy two values:
   - **Project URL** — looks like `https://abcdefghijklm.supabase.co`
   - **anon public key** — a long string starting `<your-anon-public-key>`

## 2. Paste them into the config

Edit `user/supabase-config.js`:

```js
window.SUPABASE_CONFIG = {
  url: "https://abcdefghijklm.supabase.co",
  anonKey: "<your-anon-public-key>"
};
```

The anon key is *designed* to be public — it goes in browser code by design.
Row Level Security (step 3) is what actually protects the data. Never paste the
**service_role** key here; that one bypasses all security.

## 3. Create the table and lock it down

In the Supabase dashboard open **SQL Editor** and run:

```sql
create table public.sessions (
  id          bigint generated always as identity primary key,
  user_id     uuid not null references auth.users(id) on delete cascade,
  person      text,
  started_at  timestamptz not null,
  ended_at    timestamptz,
  avg_hr      real,
  avg_breath  real,
  alert_count int  default 0,
  created_at  timestamptz default now()
);

alter table public.sessions enable row level security;

-- each account can only ever see and write its own rows
create policy "own rows: read"
  on public.sessions for select using (auth.uid() = user_id);
create policy "own rows: insert"
  on public.sessions for insert with check (auth.uid() = user_id);
create policy "own rows: update"
  on public.sessions for update using (auth.uid() = user_id);
```

## 4. Email confirmation

By default Supabase emails a confirmation link on sign-up. For a demo you can
turn that off under **Authentication → Providers → Email → Confirm email**, so
an account works immediately.

## 5. Run it

```bash
python radiobeat_server.py
```

Open <http://localhost:8770>. You should now see the sign-in screen. The
"Continue without an account" option stays available either way.

---

## Notes

- Detailed per-reading data always stays local in `radiobeat.db`. Only session
  *summaries* are meant for the cloud — that keeps the sync small and limits how
  much health data leaves the machine.
- Health data is sensitive. Do not put anyone else's readings in a cloud project
  without asking them first.
- This is a student prototype, not a medical device, and nothing here is
  suitable for clinical use.
