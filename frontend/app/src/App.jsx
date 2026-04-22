import React from "react";
import AdminView from "./views/AdminView.jsx";
import HomeView from "./views/HomeView.jsx";
import MapView from "./views/MapView.jsx";

export default function App() {
  const pathname = window.location.pathname.replace(/\/+$/, "") || "/";
  if (pathname === "/map") {
    return <MapView />;
  }
  if (pathname === "/admin") {
    return <AdminView />;
  }
  return <HomeView />;
}
