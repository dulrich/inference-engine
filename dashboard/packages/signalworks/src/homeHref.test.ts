import { describe, expect, test } from "bun:test";
import { defaultHomeHrefFor } from "./homeHref";

const at = (protocol: string, hostname: string, port = "") => defaultHomeHrefFor({ protocol, hostname, port });

describe("defaultHomeHrefFor", () => {
  test("gateway host on the default port links to center.<parent>", () => {
    expect(at("https:", "cameras.ulrichdev.com")).toBe("https://center.ulrichdev.com/");
  });
  test("center itself through the gateway stays on center", () => {
    expect(at("https:", "center.ulrichdev.com")).toBe("https://center.ulrichdev.com/");
  });
  test("LAN IP with a port is unchanged", () => {
    expect(at("http:", "192.0.2.100", "4740")).toBe("http://192.0.2.100:4730/");
  });
  test("localhost with a port is unchanged", () => {
    expect(at("http:", "localhost", "5173")).toBe("http://localhost:4730/");
  });
  test("plain-HTTP name with an explicit port is unchanged", () => {
    expect(at("http:", "center.ulrichdev.com", "4740")).toBe("http://center.ulrichdev.com:4730/");
  });
  test("IP literal on the default port falls back", () => {
    expect(at("http:", "203.0.113.3")).toBe("http://203.0.113.3:4730/");
  });
  test("localhost / two-label host on the default port fall back", () => {
    expect(at("http:", "localhost")).toBe("http://localhost:4730/");
    expect(at("https:", "ulrichdev.com")).toBe("https://ulrichdev.com:4730/");
  });
  test("IPv6 literal falls back", () => {
    expect(at("http:", "[::1]")).toBe("http://[::1]:4730/");
  });
});
