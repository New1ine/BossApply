package com.bossapply.scrolltest;

import android.content.res.Resources;
import android.os.SystemClock;
import android.view.Display;
import android.view.InputEvent;
import android.view.MotionEvent;
import android.view.ViewConfiguration;

import java.io.IOException;
import java.lang.reflect.Method;
import java.util.concurrent.atomic.AtomicBoolean;

/** Context-free shell entry, loadable from either the installed APK or cached DEX JAR. */
public final class ScrollGesture {
    private final Object manager;
    private final Method inject;
    private final AtomicBoolean cancelled = new AtomicBoolean();
    private static String refreshDetection = "fallback_60hz";

    private ScrollGesture() throws Exception {
        Class<?> type;
        try {
            type = Class.forName("android.hardware.input.InputManagerGlobal");
        } catch (ClassNotFoundException oldAndroid) {
            type = Class.forName("android.hardware.input.InputManager");
        }
        Method instance = type.getDeclaredMethod("getInstance");
        instance.setAccessible(true);
        manager = instance.invoke(null);
        inject = type.getMethod("injectInputEvent", InputEvent.class, int.class);
        inject.setAccessible(true);
    }

    private boolean inject(MotionEvent event, boolean synchronous) throws Exception {
        return Boolean.TRUE.equals(inject.invoke(manager, event, synchronous ? 2 : 0));
    }

    private void monitorInput() {
        Thread monitor = new Thread(() -> {
            try {
                // EOF or an explicit cancellation byte revokes this pointer sequence.
                System.in.read();
            } catch (IOException closed) {
                // A disconnected ADB shell is also a cancellation request.
            }
            cancelled.set(true);
        }, "gesture-control");
        monitor.setDaemon(true);
        monitor.start();
    }

    private void checkCancelled() throws InterruptedException {
        if (cancelled.get()) {
            throw new InterruptedException("gesture_control_closed");
        }
    }

    private static String quote(String value) {
        return "\"" + value.replace("\\", "\\\\").replace("\"", "\\\"")
                .replace("\r", "\\r").replace("\n", "\\n").replace("\t", "\\t") + "\"";
    }

    private static double displayRefreshRate() {
        try {
            Class<?> type = Class.forName("android.hardware.display.DisplayManagerGlobal");
            Method instance = type.getDeclaredMethod("getInstance");
            instance.setAccessible(true);
            Object displays = instance.invoke(null);
            Method realDisplay = type.getMethod("getRealDisplay", int.class);
            realDisplay.setAccessible(true);
            Object display = realDisplay.invoke(displays, 0);
            if (display instanceof Display) {
                double value = ((Display) display).getRefreshRate();
                if (Double.isFinite(value) && value >= 30 && value <= 240) {
                    refreshDetection = "display";
                    return value;
                }
            }
        } catch (Throwable unavailable) {
            // Optional platform reflection: use the same bounded fallback on older releases.
        }
        return 60;
    }

    public static void main(String[] args) {
        if (args.length == 1 && ("--describe".equals(args[0]) || "--capabilities".equals(args[0]))) {
            // Safe preflight: no InputManager instance or pointer event is created.
            System.out.println("{\"ok\":true,\"protocol\":" + GestureEngine.PROTOCOL
                    + ",\"profile\":\"uniform\",\"engine_version\":" + GestureEngine.VERSION
                    + ",\"entry\":\"com.bossapply.scrolltest.ScrollGesture\"}");
            return;
        }
        long started = SystemClock.uptimeMillis();
        GestureEngine.Result result = null;
        String error = null;
        int duration = 0;
        int hold = 0;
        double distance = 0;
        int touchSlop = 0;
        String refreshSource = "fallback_60hz";
        try {
            boolean monitorStdin = args.length > 0 && "--monitor-stdin".equals(args[args.length - 1]);
            int argumentCount = args.length - (monitorStdin ? 1 : 0);
            if (argumentCount != 6 && argumentCount != 7) {
                throw new IllegalArgumentException("Expected x1 y1 x2 y2 durationMs holdMs [refreshHz] [--monitor-stdin]");
            }
            int x1 = Integer.parseInt(args[0]);
            int y1 = Integer.parseInt(args[1]);
            int x2 = Integer.parseInt(args[2]);
            int y2 = Integer.parseInt(args[3]);
            duration = Integer.parseInt(args[4]);
            hold = Integer.parseInt(args[5]);
            double refreshHz = argumentCount == 7 ? Double.parseDouble(args[6]) : displayRefreshRate();
            refreshSource = argumentCount == 7 ? "argument" : refreshDetection;
            distance = Math.hypot((double) x2 - x1, (double) y2 - y1);
            // app_process has no application Context. Preserve the conservative
            // density-scaled estimate, not a claim to the target app's exact slop.
            touchSlop = Math.max(8, (int) Math.ceil(8 * Resources.getSystem().getDisplayMetrics().density));
            ScrollGesture shell = new ScrollGesture();
            if (monitorStdin) {
                shell.monitorInput();
            }
            result = GestureEngine.run(shell::inject, shell::checkCancelled,
                    x1, y1, x2, y2, duration, hold, refreshHz, touchSlop,
                    Math.min(200, ViewConfiguration.getLongPressTimeout() / 2));
        } catch (Throwable failure) {
            if (failure instanceof GestureEngine.Failure) {
                result = ((GestureEngine.Failure) failure).progress;
            }
            Throwable cause = failure.getCause() == null ? failure : failure.getCause();
            error = cause.getClass().getSimpleName() + ": " + String.valueOf(cause.getMessage());
        }
        String fields = result == null
                ? "\"engine_version\":" + GestureEngine.VERSION + ",\"elapsed_ms\":"
                    + (SystemClock.uptimeMillis() - started) + ",\"events\":0,\"requested_duration_ms\":"
                    + duration + ",\"terminal_hold_ms\":" + hold + ",\"distance_px\":" + distance
                    + ",\"sampler\":null"
                : result.jsonFields();
        String response = "{\"protocol\":" + GestureEngine.PROTOCOL + ",\"profile\":\"uniform\",\"ok\":"
                + (error == null) + "," + fields + ",\"estimated_touch_slop_px\":" + touchSlop
                + ",\"refresh_source\":" + quote(refreshSource)
                + ",\"error\":" + (error == null ? "null" : quote(error)) + "}";
        System.out.println(response);
        if (error != null) {
            System.err.println(response);
            System.exit(1);
        }
    }
}
