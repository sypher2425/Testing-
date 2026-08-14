import type { Config } from "tailwindcss";

const config: Config = {
  darkMode: "class",
  content: ["./app/**/*.{ts,tsx}", "./components/**/*.{ts,tsx}"],
  theme: {
    extend: {
      colors: {
        surface: {
          DEFAULT: "#050706",
          raised: "#0c100d",
          border: "#263029",
        },
      },
    },
  },
  plugins: [],
};

export default config;
