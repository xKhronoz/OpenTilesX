import React from "react";
import { createRoot } from "react-dom/client";
import App from "./App.jsx";
import "./styles/common.css";
import "./styles/app.css";
import "./styles/map.css";

createRoot(document.getElementById("root")).render(
  <React.StrictMode>
    <App />
  </React.StrictMode>,
);
