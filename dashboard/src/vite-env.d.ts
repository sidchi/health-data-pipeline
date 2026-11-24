/// <reference types="vite/client" />

interface ImportMetaEnv {
  /** Where to fetch the --daily JSON from. Defaults to the bundled synthetic sample. */
  readonly VITE_DAILY_URL?: string;
}
