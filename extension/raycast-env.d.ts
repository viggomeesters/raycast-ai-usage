/// <reference types="@raycast/api">

/* 🚧 🚧 🚧
 * This file is auto-generated from the extension's manifest.
 * Do not modify manually. Instead, update the `package.json` file.
 * 🚧 🚧 🚧 */

/* eslint-disable @typescript-eslint/ban-types */

type ExtensionPreferences = {
  /** Backend Path - Optional absolute path to the ai-usage executable */
  "backendPath"?: string
}

/** Preferences accessible in all the extension's commands */
declare type Preferences = ExtensionPreferences

declare namespace Preferences {
  /** Preferences accessible in the `ai-usage` command */
  export type AiUsage = ExtensionPreferences & {}
}

declare namespace Arguments {
  /** Arguments passed to the `ai-usage` command */
  export type AiUsage = {}
}
