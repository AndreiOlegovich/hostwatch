export const dateFormat = new Intl.DateTimeFormat(undefined, { day: "numeric", month: "short", year: "numeric" });
export const shortDateFormat = new Intl.DateTimeFormat(undefined, { day: "numeric", month: "short" });
export const timeFormat = new Intl.DateTimeFormat(undefined, { hour: "2-digit", minute: "2-digit" });

async function readResponse(response) {
  if (!response.ok) {
    let message = `${response.status} ${response.statusText}`;
    try {
      message = (await response.json()).detail || message;
    } catch {
      // keep the status line
    }
    throw new Error(message);
  }
  return response.json();
}

export async function getJSON(url) {
  return readResponse(await fetch(url));
}

// The X-Hostwatch header is what the server checks to refuse changes sent from other web pages.
export async function postJSON(url, body) {
  return readResponse(
    await fetch(url, {
      method: "POST",
      headers: { "Content-Type": "application/json", "X-Hostwatch": "1" },
      body: JSON.stringify(body),
    }),
  );
}

export function sameDay(a, b) {
  return a.toDateString() === b.toDateString();
}

export function plural(n, word) {
  return `${n} ${word}${n === 1 ? "" : "s"}`;
}
