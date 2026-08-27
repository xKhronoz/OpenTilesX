import React from "react";
import AdminView from "./views/AdminView.jsx";
import HomeView from "./views/HomeView.jsx";
import MapView from "./views/MapView.jsx";
import { appRouteFromPathname } from "./utils/routes.js";

export default function App() {
  const pathname = appRouteFromPathname(window.location.pathname);
  if (pathname === "/map") {
    return <MapView />;
  }
  if (pathname === "/admin") {
    return <AdminView />;
  }
  return <HomeView />;
}
