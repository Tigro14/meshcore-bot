import html from "eslint-plugin-html";
import globals from "globals";

export default [
  {
    files: ["**/*.{html,js}"],
    plugins: { html },
    languageOptions: {
      ecmaVersion: 2021,
      sourceType: "script",
      globals: {
        ...globals.browser,
        io: "readonly",
        bootstrap: "readonly",
        Chart: "readonly",
        L: "readonly",
        escapeHtml: "writable",
        socket: "writable",
        // Vendored UMD library (static/vendor/qrcode-generator/qrcode.js), loaded
        // with a plain <script src> by the channels page.
        qrcode: "readonly",
        // Declared by static/js/channel_operations.js; pages that load it call it.
        MeshCoreChannelOps: "readonly",
        // Declared in base.html; page scripts that extend it read it.
        IS_ADMIN: "readonly",
      },
    },
    settings: {
      "html/html-extensions": [".html"],
    },
    rules: {
      "no-undef": "warn",
      "no-unused-vars": "warn",
      "no-console": "off",
      semi: ["warn", "always"],
      eqeqeq: ["warn", "always"],
      "no-eval": "error",
      "no-implied-eval": "error",
    },
  },
];
