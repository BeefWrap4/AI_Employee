// SSE (Server-Sent Events) subscriber for the web portal.
//
// The agent-platform-api exposes a live run stream at
//   GET /api/v1/agent-runs/{run_id}/stream   (port 8030)
// which yields `data: {json}\n\n` frames, each a serialized RunEvent
// {run_id, event_type, payload, ts}.  In dev the vite proxy maps
// `/api/platform` -> http://127.0.0.1:8030 (stripping the prefix), so
// the browser-side URL is
//   /api/platform/api/v1/agent-runs/{run_id}/stream
//
// EventSource is native to all evergreen browsers; no polyfill needed.
// The browser parses the SSE frames and fires `onmessage` with
// `event.data` set to the frame's data payload (the JSON string).

const RUN_STREAM_PREFIX = '/api/platform/api/v1/agent-runs'

/**
 * Open an SSE subscription to a run's live event stream.
 *
 * @param {string} runId - The agent run id to watch.
 * @param {(event: object) => void} onEvent - Called once per RunEvent (parsed JSON).
 * @param {(err: Event) => void} [onError] - Called if the stream errors; the EventSource is then closed.
 * @returns {() => void} cleanup - Call to close the EventSource and stop receiving events.
 */
export function subscribeRunStream(runId, onEvent, onError) {
  const url = `${RUN_STREAM_PREFIX}/${runId}/stream`
  const es = new EventSource(url)

  es.onmessage = (messageEvent) => {
    const raw = messageEvent.data
    let parsed
    try {
      parsed = JSON.parse(raw)
    } catch (e) {
      // Malformed frame — surface as an error but keep the stream open;
      // the browser will auto-reconnect EventSource on transient issues.
      if (onError) onError(e)
      return
    }
    onEvent(parsed)
  }

  es.onerror = (err) => {
    if (onError) onError(err)
    // EventSource auto-reconnects by default; for a finite run stream we
    // close on error to avoid reconnect loops once the server ends the
    // stream.  Callers may re-subscribe if they want to retry.
    es.close()
  }

  return () => {
    es.close()
  }
}
