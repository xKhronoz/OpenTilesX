export function appPath(path = "/") {
  if (path === "/" || path === "") return ".";
  return path.replace(/^\/+/, "");
}

export function appRouteFromPathname(pathname) {
  const trimmed = (pathname || "/").replace(/\/+$/, "") || "/";
  if (trimmed === "/") {
    return "/";
  }

  const segments = trimmed.split("/").filter(Boolean);
  const routeName = [...segments]
    .reverse()
    .find((segment) => segment === "map" || segment === "admin");
  return routeName ? `/${routeName}` : "/";
}
