package com.bossapply.scrolltest;

import android.os.SystemClock;
import android.view.InputDevice;
import android.view.MotionEvent;

import java.io.IOException;
import java.util.Arrays;

/** One uniform-drag implementation for instrumentation and shell transports. */
public final class GestureEngine {
    public static final int VERSION = 1;
    public static final int PROTOCOL = 1;
    public static final int END_HOLD_MS = 150;

    public interface Injector {
        boolean inject(MotionEvent event, boolean synchronous) throws Exception;
    }

    public interface Cancellation {
        void check() throws Exception;
    }

    private final Injector injector;
    private final Cancellation cancellation;
    private final MotionEvent.PointerProperties[] pointers = {new MotionEvent.PointerProperties()};
    private final MotionEvent.PointerCoords[] coordinates = {new MotionEvent.PointerCoords()};
    private long downTime;
    private float lastX;
    private float lastY;
    private int events;
    private Result progress;
    private long motionStartMs;
    private boolean motionFinished;

    public static final class Failure extends IOException {
        public final Result progress;

        private Failure(Exception cause, Result progress) {
            super(cause.getMessage(), cause);
            this.progress = progress;
        }
    }

    private GestureEngine(Injector injector, Cancellation cancellation) {
        this.injector = injector;
        this.cancellation = cancellation;
        pointers[0].id = 0;
        pointers[0].toolType = MotionEvent.TOOL_TYPE_FINGER;
    }

    public static Result run(Injector injector, Cancellation cancellation,
                             int x1, int y1, int x2, int y2, int durationMs, int holdMs,
                             double refreshHz, int touchSlop, int startGuardMs) throws Exception {
        double distance = Math.hypot((double) x2 - x1, (double) y2 - y1);
        if (x1 < 0 || y1 < 0 || x2 < 0 || y2 < 0 || distance == 0
                || durationMs < 50 || durationMs > 5000 || holdMs < END_HOLD_MS || holdMs > 1000
                || !Double.isFinite(refreshHz) || refreshHz < 30 || refreshHz > 240 || touchSlop < 1) {
            throw new IOException("invalid_gesture_arguments");
        }
        if (distance < 2.0 * touchSlop) {
            throw new IOException("gesture_path_below_drag_threshold");
        }
        double thresholdTime = durationMs * (2.0 * touchSlop / distance);
        if (startGuardMs < 1 || thresholdTime > startGuardMs) {
            throw new IOException("uniform_drag_start_would_be_too_slow");
        }
        GestureEngine engine = new GestureEngine(injector, cancellation);
        try {
            return engine.perform(x1, y1, x2, y2, durationMs, holdMs, refreshHz,
                    distance, touchSlop, startGuardMs, thresholdTime);
        } catch (Exception failure) {
            if (failure instanceof InterruptedException) {
                Thread.currentThread().interrupt();
            }
            throw new Failure(failure, engine.progress);
        }
    }

    private void send(int action, float x, float y, boolean synchronous) throws Exception {
        MotionEvent.PointerCoords coords = coordinates[0];
        coords.x = x;
        coords.y = y;
        coords.pressure = action == MotionEvent.ACTION_UP || action == MotionEvent.ACTION_CANCEL ? 0 : 1;
        coords.size = 1;
        MotionEvent event = MotionEvent.obtain(downTime, SystemClock.uptimeMillis(), action, 1,
                pointers, coordinates, 0, 0, 1, 1, 0, 0, InputDevice.SOURCE_TOUCHSCREEN, 0);
        // If acknowledgement fails, cancellation still uses the attempted point.
        lastX = x;
        lastY = y;
        try {
            if (!injector.inject(event, synchronous)) {
                throw new IOException("gesture_injection_unconfirmed");
            }
            events++;
        } finally {
            event.recycle();
        }
    }

    private void waitUntil(long deadlineNanos) throws Exception {
        while (true) {
            cancellation.check();
            long remaining = deadlineNanos - System.nanoTime();
            if (remaining <= 0) {
                return;
            }
            remaining = Math.min(remaining, 16000000L);
            Thread.sleep(remaining / 1000000L, (int) (remaining % 1000000L));
        }
    }

    private Result perform(int x1, int y1, int x2, int y2, int durationMs, int holdMs,
                           double refreshHz, double distance, int touchSlop,
                           int startGuardMs, double thresholdTime) throws Exception {
        Result result = new Result(durationMs, holdMs, refreshHz, distance, touchSlop, thresholdTime);
        progress = result;
        boolean down = false;
        boolean up = false;
        downTime = SystemClock.uptimeMillis();
        lastX = x1;
        lastY = y1;
        try {
            cancellation.check();
            down = true;
            send(MotionEvent.ACTION_DOWN, x1, y1, true);
            cancellation.check();
            long motionStarted = SystemClock.uptimeMillis();
            motionStartMs = motionStarted;
            result.startDelayMs = motionStarted - downTime;
            if (result.startDelayMs > startGuardMs) {
                throw new IOException("gesture_down_acknowledgement_delayed");
            }
            long motionNs = System.nanoTime();
            long durationNs = durationMs * 1000000L;
            long endNs = motionNs + durationNs;
            double periodNs = 1000000000.0 / refreshHz;
            long slot = 1;
            while (true) {
                cancellation.check();
                long now = System.nanoTime();
                long deadline = motionNs + Math.round(slot * periodNs);
                if (deadline < now) {
                    long futureSlot = (long) Math.floor((now - motionNs) / periodNs) + 1;
                    result.timing.skippedSlots += Math.max(0, futureSlot - slot);
                    slot = futureSlot;
                    deadline = motionNs + Math.round(slot * periodNs);
                }
                if (deadline >= endNs) {
                    break;
                }
                waitUntil(deadline);
                long dispatched = System.nanoTime();
                if (dispatched >= endNs) {
                    break;
                }
                long elapsedSlot = (long) Math.floor((dispatched - motionNs) / periodNs);
                if (elapsedSlot > slot) {
                    result.timing.skippedSlots += elapsedSlot - slot;
                    slot = elapsedSlot;
                }
                double fraction = Math.min(1.0, (dispatched - motionNs) / (double) durationNs);
                send(MotionEvent.ACTION_MOVE, (float) (x1 + (x2 - x1) * fraction),
                        (float) (y1 + (y2 - y1) * fraction), false);
                result.timing.record(dispatched, System.nanoTime() - dispatched,
                        Math.max(0, dispatched - deadline));
                slot++;
            }
            waitUntil(endNs);
            send(MotionEvent.ACTION_MOVE, x2, y2, true);
            result.motionElapsedMs = SystemClock.uptimeMillis() - motionStarted;
            motionFinished = true;
            waitUntil(System.nanoTime() + holdMs * 1000000L);
            send(MotionEvent.ACTION_MOVE, x2, y2, true);
            cancellation.check();
            send(MotionEvent.ACTION_UP, x2, y2, true);
            up = true;
        } finally {
            if (down && !up) {
                try {
                    send(MotionEvent.ACTION_CANCEL, lastX, lastY, true);
                } catch (Exception ignored) {
                    // Preserve the original failure; an uncertain action is never replayed.
                }
            }
            result.elapsedMs = SystemClock.uptimeMillis() - downTime;
            result.events = events;
            if (motionStartMs != 0 && !motionFinished) {
                result.motionElapsedMs = SystemClock.uptimeMillis() - motionStartMs;
            }
        }
        return result;
    }

    public static final class Result {
        public long elapsedMs;
        public int events;
        public long motionElapsedMs;
        public long startDelayMs;
        public final int durationMs;
        public final int holdMs;
        public final double refreshHz;
        public final double distance;
        public final int touchSlop;
        public final double thresholdTime;
        private final Timing timing = new Timing();

        private Result(int durationMs, int holdMs, double refreshHz, double distance,
                       int touchSlop, double thresholdTime) {
            this.durationMs = durationMs;
            this.holdMs = holdMs;
            this.refreshHz = refreshHz;
            this.distance = distance;
            this.touchSlop = touchSlop;
            this.thresholdTime = thresholdTime;
        }

        /** Fields retain the existing reader and shell telemetry schemas. */
        public String jsonFields() {
            return "\"engine_version\":" + VERSION + ",\"elapsed_ms\":" + elapsedMs
                    + ",\"events\":" + events + ",\"duration_ms\":" + durationMs
                    + ",\"requested_duration_ms\":" + durationMs
                    + ",\"motion_profile\":\"uniform\",\"distance_px\":" + distance
                    + ",\"drag_distance_px\":" + distance
                    + ",\"requested_speed_px_s\":" + distance * 1000 / durationMs
                    + ",\"drag_speed_px_per_s\":" + distance * 1000 / durationMs
                    + ",\"down_ack_ms\":" + startDelayMs + ",\"start_delay_ms\":" + startDelayMs
                    + ",\"motion_elapsed_ms\":" + motionElapsedMs + ",\"drag_elapsed_ms\":" + motionElapsedMs
                    + ",\"drag_threshold_time_ms\":" + thresholdTime
                    + ",\"touch_slop_px\":" + touchSlop + ",\"terminal_hold_ms\":" + holdMs
                    + ",\"cadence_target_hz\":" + refreshHz + timing.legacyFields()
                    + ",\"sampler\":" + timing.samplerJson(refreshHz);
        }
    }

    private static final class Timing {
        private final long[] intervals = new long[1208];
        private final long[] calls = new long[1208];
        private int intervalCount;
        private int moveCount;
        private long firstMove;
        private long lastMove;
        private long latenessMax;
        private long lateSlots;
        private long skippedSlots;

        void record(long sent, long callDuration, long lateness) {
            if (moveCount > 0 && intervalCount < intervals.length) {
                intervals[intervalCount++] = sent - lastMove;
            }
            if (moveCount < calls.length) {
                calls[moveCount] = callDuration;
            }
            if (firstMove == 0) {
                firstMove = sent;
            }
            lastMove = sent;
            moveCount++;
            latenessMax = Math.max(latenessMax, lateness);
            if (lateness >= 1000000L) {
                lateSlots++;
            }
        }

        private double observedHz() {
            return moveCount > 1 && lastMove > firstMove
                    ? (moveCount - 1) * 1000000000.0 / (lastMove - firstMove) : 0;
        }

        String legacyFields() {
            return ",\"timing_scope\":\"move_dispatch_not_rendered_frames\""
                    + ",\"periodic_move_count\":" + moveCount + ",\"move_interval_count\":" + intervalCount
                    + ",\"move_interval_mean_ms\":" + mean(intervals, intervalCount)
                    + ",\"move_interval_p50_ms\":" + percentile(intervals, intervalCount, .5)
                    + ",\"move_interval_p95_ms\":" + percentile(intervals, intervalCount, .95)
                    + ",\"move_interval_max_ms\":" + percentile(intervals, intervalCount, 1)
                    + ",\"move_dispatch_hz\":" + observedHz() + ",\"skipped_move_slots\":" + skippedSlots
                    + ",\"move_injection_mean_ms\":" + mean(calls, moveCount)
                    + ",\"move_injection_max_ms\":" + percentile(calls, moveCount, 1)
                    + ",\"move_lateness_max_ms\":" + latenessMax / 1000000.0;
        }

        String samplerJson(double refreshHz) {
            return "{\"clock\":\"absolute_monotonic\",\"move_sync\":false,\"target_hz\":" + refreshHz
                    + ",\"periodic_move_events\":" + moveCount + ",\"observed_send_hz\":" + observedHz()
                    + ",\"late_slots\":" + lateSlots + ",\"late_threshold_ms\":1,\"skipped_slots\":" + skippedSlots
                    + ",\"move_intervals\":" + statistics(intervals, intervalCount)
                    + ",\"move_calls\":" + statistics(calls, moveCount)
                    + ",\"note\":\"Injection call timing only; not rendered frame rate\"}";
        }
    }

    private static double mean(long[] values, int count) {
        count = Math.min(count, values.length);
        double total = 0;
        for (int index = 0; index < count; index++) {
            total += values[index];
        }
        return count == 0 ? 0 : total / count / 1000000.0;
    }

    private static double percentile(long[] values, int count, double percentile) {
        count = Math.min(count, values.length);
        if (count == 0) {
            return 0;
        }
        long[] sorted = Arrays.copyOf(values, count);
        Arrays.sort(sorted);
        return sorted[(int) Math.ceil(count * percentile) - 1] / 1000000.0;
    }

    private static String statistics(long[] values, int count) {
        return "{\"count\":" + count + ",\"mean_ms\":" + mean(values, count)
                + ",\"p50_ms\":" + percentile(values, count, .5)
                + ",\"p95_ms\":" + percentile(values, count, .95)
                + ",\"max_ms\":" + percentile(values, count, 1) + "}";
    }
}
