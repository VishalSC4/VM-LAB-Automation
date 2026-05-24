import type { Config } from "tailwindcss";

const config: Config = {
  content: ["./app/**/*.{ts,tsx}", "./components/**/*.{ts,tsx}"],
  theme: {
    extend: {
      colors: {
        ink: "#17202A",
        line: "#D9E2EC",
        mist: "#F6F8FA",
        coral: "#E85D75",
        teal: "#177E89",
        amber: "#F2B84B",
        "unext-orange": "#FF7A00",
        "unext-red": "#F6281D"
      },
      boxShadow: {
        "3d": "0 24px 70px rgba(255, 122, 0, 0.18), 0 10px 30px rgba(23, 32, 42, 0.08)",
        button: "0 10px 0 #C94A10, 0 18px 28px rgba(255, 122, 0, 0.28)",
        soft: "0 10px 25px rgba(23, 32, 42, 0.08)"
      }
    }
  },
  plugins: []
};

export default config;
