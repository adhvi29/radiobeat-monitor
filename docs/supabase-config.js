// RadioBeat — Supabase account support.
//
// This is the ANON (public) key. Supabase designs it to ship in browser code:
// it can only ever do what the Row Level Security policies allow, and those
// restrict every row to auth.uid() = user_id. See SUPABASE_SETUP.md.
//
// Only the ANON (public) key belongs here. It is safe in browser code by
// design: Row Level Security is what actually protects the data.
//
// NEVER put a key starting with "sb_secret_" or the service_role key in this
// file — those bypass every RLS policy and would expose the whole project.
//
// To run with no account at all, blank both values out.

window.SUPABASE_CONFIG = {
  url: "https://inhcecuimdrygndlfxtk.supabase.co",
  anonKey: "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJpc3MiOiJzdXBhYmFzZSIsInJlZiI6ImluaGNlY3VpbWRyeWduZGxmeHRrIiwicm9sZSI6ImFub24iLCJpYXQiOjE3ODgzNDYwMzQsImV4cCI6MjEwMzkyMjAzNH0.naVzJv3ZE5oIe57M_DnCMf-Bo3iSlCG6CeGS7N1wD3w"
};
