import i18n from "i18next";
import { initReactI18next } from "react-i18next";
import en from "./en.json";

// Add a language: create <code>.json next to en.json and register it here.
void i18n.use(initReactI18next).init({
  resources: { en: { translation: en } },
  lng: localStorage.getItem("plainboard.lang") ?? "en",
  fallbackLng: "en",
  interpolation: { escapeValue: false },
});

export default i18n;
