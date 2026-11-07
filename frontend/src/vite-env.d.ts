/// <reference types="vite/client" />

interface ImportMetaEnv {
  readonly VITE_API_URL?: string;
  readonly VITE_SUPABASE_URL?: string;
  readonly VITE_SUPABASE_ANON_KEY?: string;
  /** '1' when building the Android app (remote client for a Ghost PC). */
  readonly VITE_GHOST_MOBILE?: string;
  /** App version and build label, set by CI (see build-apps.yml). */
  readonly VITE_GHOST_VERSION?: string;
  readonly VITE_GHOST_BUILD?: string;
}

interface ImportMeta {
  readonly env: ImportMetaEnv;
}
