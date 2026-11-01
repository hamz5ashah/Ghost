/// <reference types="vite/client" />

interface ImportMetaEnv {
  readonly VITE_API_URL?: string;
  readonly VITE_SUPABASE_URL?: string;
  readonly VITE_SUPABASE_ANON_KEY?: string;
  /** '1' when building the Android app (remote client for a Ghost PC). */
  readonly VITE_GHOST_MOBILE?: string;
}

interface ImportMeta {
  readonly env: ImportMetaEnv;
}
