package com.bossapply.uiprobe;

import android.accessibilityservice.AccessibilityServiceInfo;
import android.app.Activity;
import android.app.Instrumentation;
import android.app.UiAutomation;
import android.graphics.Point;
import android.graphics.Rect;
import android.net.LocalServerSocket;
import android.net.LocalSocket;
import android.os.Bundle;
import android.os.Handler;
import android.os.Looper;
import android.os.SystemClock;
import android.system.ErrnoException;
import android.system.Os;
import android.system.OsConstants;
import android.system.StructPollfd;
import android.util.Xml;
import android.view.ViewConfiguration;
import android.view.WindowManager;
import android.view.accessibility.AccessibilityEvent;
import android.view.accessibility.AccessibilityNodeInfo;

import org.xmlpull.v1.XmlSerializer;

import com.bossapply.scrolltest.GestureEngine;

import java.io.ByteArrayOutputStream;
import java.io.DataInputStream;
import java.io.DataOutputStream;
import java.io.IOException;
import java.io.OutputStream;
import java.nio.charset.StandardCharsets;
import java.security.MessageDigest;
import java.util.concurrent.TimeoutException;
import java.util.concurrent.atomic.AtomicBoolean;

public final class ProbeInstrumentation extends Instrumentation {
    private static final String BOSS_PACKAGE = "com.hpbr.bosszhipin";
    private static final int MAX_XML_BYTES = 8 * 1024 * 1024;
    private static final int MAX_TREE_DEPTH = 128;
    private static final int STARTUP_LIMIT_MS = 15000;
    private static final int INACTIVITY_LIMIT_MS = 45000;
    private static final int REQUEST_LIMIT_MS = 15000;
    private static final int STRUCTURAL_QUIET_MS = 55;
    private static final int QUIET_WAIT_LIMIT_MS = 160;
    private static final int SCROLL_STRUCTURAL_QUIET_MS = 80;
    private static final int SCROLL_QUIET_MS = 120;
    private static final int SCROLL_WAIT_LIMIT_MS = 800;
    private static final int GESTURE_END_HOLD_MS = 150;
    private static final int GESTURE_START_GUARD_MS = 200;
    private static final int GLOBAL_WINDOW_EVENTS = AccessibilityEvent.TYPE_WINDOW_STATE_CHANGED
            | AccessibilityEvent.TYPE_WINDOWS_CHANGED;
    private static final int CONTENT_EVENTS = AccessibilityEvent.TYPE_WINDOW_CONTENT_CHANGED
            | AccessibilityEvent.TYPE_VIEW_SCROLLED;
    private static final byte[] READY = "READY\n".getBytes(StandardCharsets.US_ASCII);
    private static final Capture MOVING = Capture.retry("moving");
    private static final Capture CHANGED = Capture.retry("changed");
    private static final Capture UNAVAILABLE = Capture.retry("unavailable");
    private static final Capture FOREIGN = Capture.retry("foreign");

    private final AtomicBoolean ended = new AtomicBoolean();
    private final Object windowEvents = new Object();
    private final Handler handler = new Handler(Looper.getMainLooper());
    private final Runnable watchdog = () -> complete(Activity.RESULT_CANCELED);
    private String socketName;
    private byte[] tokenLine;
    private boolean adaptive;
    private boolean scrollAware;
    private boolean gestureAware;
    private volatile long changeSequence;
    private long scrollSequence;
    private long lastWindowChange;
    private long lastScrollChange;

    private static final class Capture {
        final int status;
        final byte[] payload;

        Capture(int status, byte[] payload) {
            this.status = status;
            this.payload = payload;
        }

        static Capture retry(String reason) {
            return new Capture(1, reason.getBytes(StandardCharsets.US_ASCII));
        }
    }

    private static final class WindowSnapshot {
        final long changes;
        final long scrolls;

        WindowSnapshot(long changes, long scrolls) {
            this.changes = changes;
            this.scrolls = scrolls;
        }
    }

    @Override
    public void onCreate(Bundle args) {
        super.onCreate(args);
        socketName = args == null ? null : args.getString("socket_name");
        String token = args == null ? null : args.getString("token");
        if (socketName == null || !socketName.matches("[a-z0-9_-]{8,80}")
                || token == null || !token.matches("[a-z0-9_-]{16,128}")) {
            complete(Activity.RESULT_CANCELED);
            return;
        }
        tokenLine = (token + "\n").getBytes(StandardCharsets.US_ASCII);
        String protocol = args.getString("protocol");
        gestureAware = "8".equals(protocol) || "7".equals(protocol) || "6".equals(protocol) || "5".equals(protocol);
        scrollAware = gestureAware || "4".equals(protocol);
        adaptive = scrollAware || "3".equals(protocol);
        handler.postDelayed(watchdog, STARTUP_LIMIT_MS);
        start();
    }

    @Override
    public void onStart() {
        int result = Activity.RESULT_CANCELED;
        try (LocalServerSocket server = new LocalServerSocket(socketName)) {
            UiAutomation automation = getUiAutomation(UiAutomation.FLAG_DONT_SUPPRESS_ACCESSIBILITY_SERVICES);
            if (automation == null) {
                return;
            }
            AccessibilityServiceInfo info = automation.getServiceInfo();
            info.flags |= AccessibilityServiceInfo.FLAG_REPORT_VIEW_IDS
                    | AccessibilityServiceInfo.FLAG_INCLUDE_NOT_IMPORTANT_VIEWS;
            if (adaptive) {
                info.flags |= AccessibilityServiceInfo.FLAG_RETRIEVE_INTERACTIVE_WINDOWS;
                info.eventTypes |= GLOBAL_WINDOW_EVENTS | CONTENT_EVENTS;
            }
            automation.setServiceInfo(info);
            automation.setOnAccessibilityEventListener(this::onWindowEvent);
            while (!ended.get()) {
                try (LocalSocket client = server.accept()) {
                    client.setSoTimeout(INACTIVITY_LIMIT_MS);
                    DataInputStream input = new DataInputStream(client.getInputStream());
                    byte[] supplied = new byte[tokenLine.length];
                    try {
                        input.readFully(supplied);
                    } catch (IOException disconnected) {
                        continue;
                    }
                    if (!MessageDigest.isEqual(tokenLine, supplied)) {
                        continue;
                    }
                    handler.removeCallbacks(watchdog);
                    DataOutputStream output = new DataOutputStream(client.getOutputStream());
                    output.write(READY);
                    output.flush();
                    readRequests(automation, client, input, output);
                    result = Activity.RESULT_OK;
                    break;
                }
            }
        } catch (IOException | RuntimeException failure) {
            // The desktop observes the closed connection and stops the job.
        } finally {
            complete(result);
        }
    }

    private void onWindowEvent(AccessibilityEvent event) {
        int type = event.getEventType();
        boolean windowChanged = (type & GLOBAL_WINDOW_EVENTS) != 0;
        CharSequence packageName = event.getPackageName();
        boolean bossContentChanged = (type & CONTENT_EVENTS) != 0
                && packageName != null && BOSS_PACKAGE.contentEquals(packageName);
        if (!windowChanged && !bossContentChanged) {
            return;
        }
        synchronized (windowEvents) {
            changeSequence++;
            lastWindowChange = SystemClock.uptimeMillis();
            if (bossContentChanged && (type & AccessibilityEvent.TYPE_VIEW_SCROLLED) != 0) {
                scrollSequence++;
                lastScrollChange = lastWindowChange;
            }
            windowEvents.notifyAll();
        }
    }

    private WindowSnapshot awaitQuietWindow(boolean afterScroll) throws IOException {
        int quietTime = afterScroll ? SCROLL_STRUCTURAL_QUIET_MS : STRUCTURAL_QUIET_MS;
        int waitLimit = afterScroll ? SCROLL_WAIT_LIMIT_MS : QUIET_WAIT_LIMIT_MS;
        long deadline = SystemClock.uptimeMillis() + waitLimit;
        synchronized (windowEvents) {
            while (true) {
                long now = SystemClock.uptimeMillis();
                long quietAt = lastWindowChange + quietTime;
                if (afterScroll) {
                    quietAt = Math.max(quietAt, lastScrollChange + SCROLL_QUIET_MS);
                }
                long untilQuiet = quietAt - now;
                if (untilQuiet <= 0) {
                    return new WindowSnapshot(changeSequence, scrollSequence);
                }
                long remaining = deadline - now;
                if (remaining <= 0) {
                    return null;
                }
                try {
                    windowEvents.wait(Math.min(untilQuiet, remaining));
                } catch (InterruptedException interrupted) {
                    Thread.currentThread().interrupt();
                    throw new IOException("Interrupted while waiting for the window", interrupted);
                }
            }
        }
    }

    private void readRequests(UiAutomation automation, LocalSocket client,
                              DataInputStream input, DataOutputStream output)
            throws IOException {
        while (!ended.get()) {
            int request = input.read();
            boolean afterScroll = scrollAware && request == 'S';
            boolean gesture = gestureAware && request == 'G';
            if (request != 'D' && !afterScroll && !gesture) {
                return;
            }
            handler.postDelayed(watchdog, REQUEST_LIMIT_MS);
            try {
                if (gesture) {
                    int x1 = input.readInt();
                    int y1 = input.readInt();
                    int x2 = input.readInt();
                    int y2 = input.readInt();
                    int duration = input.readInt();
                    try {
                        writeFrame(output, 0, performGesture(automation, client, x1, y1, x2, y2, duration));
                    } catch (IOException | RuntimeException rejected) {
                        // This is an action result, never a retryable read transition.
                        String message = rejected.getMessage();
                        writeFrame(output, 2, (message == null ? "gesture_failed" : message)
                                .getBytes(StandardCharsets.UTF_8));
                    }
                    continue;
                }
                Capture result;
                try {
                    result = capture(automation, afterScroll);
                } catch (IOException | RuntimeException invalid) {
                    writeFrame(output, 2, null);
                    continue;
                }
                byte[] payload = result.status == 1 && !scrollAware ? null : result.payload;
                writeFrame(output, result.status, payload);
            } finally {
                handler.removeCallbacks(watchdog);
            }
        }
    }

    @SuppressWarnings("deprecation")
    private byte[] performGesture(UiAutomation automation, LocalSocket client,
                                  int x1, int y1, int x2, int y2, int duration) throws IOException {
        WindowManager windows = getContext().getSystemService(WindowManager.class);
        Point screen = new Point();
        windows.getDefaultDisplay().getRealSize(screen);
        if (duration < 50 || duration > 5000 || x1 < 0 || x1 >= screen.x || x2 < 0 || x2 >= screen.x
                || y1 < 0 || y1 >= screen.y || y2 < 0 || y2 >= screen.y || (x1 == x2 && y1 == y2)) {
            throw new IOException("invalid_gesture_bounds");
        }
        AccessibilityNodeInfo root = automation.getRootInActiveWindow();
        if (root == null) {
            throw new IOException("active_window_unavailable");
        }
        try {
            CharSequence packageName = root.getPackageName();
            if (packageName == null || !BOSS_PACKAGE.contentEquals(packageName)) {
                throw new IOException("foreground_is_not_boss");
            }
        } finally {
            root.recycle();
        }
        int touchSlop = ViewConfiguration.get(getContext()).getScaledTouchSlop();
        float refreshRate = windows.getDefaultDisplay().getRefreshRate();
        boolean displayCadence = !Float.isNaN(refreshRate) && !Float.isInfinite(refreshRate)
                && refreshRate >= 30 && refreshRate <= 240;
        double targetHz = displayCadence ? refreshRate : 60.0;
        try {
            GestureEngine.Result result = GestureEngine.run(
                    (event, synchronous) -> automation.injectInputEvent(event, synchronous),
                    () -> checkGestureConnection(client),
                    x1, y1, x2, y2, duration, GESTURE_END_HOLD_MS, targetHz, touchSlop,
                    Math.min(GESTURE_START_GUARD_MS, ViewConfiguration.getLongPressTimeout() / 2));
            String response = "{\"ok\":true,\"protocol\":" + GestureEngine.PROTOCOL
                    + ",\"profile\":\"uniform\"," + result.jsonFields()
                    + ",\"cadence_source\":\"" + (displayCadence ? "display_refresh_rate" : "fallback_60hz") + "\"}";
            return response.getBytes(StandardCharsets.US_ASCII);
        } catch (IOException failure) {
            throw failure;
        } catch (InterruptedException interrupted) {
            Thread.currentThread().interrupt();
            throw new IOException("gesture_schedule_interrupted", interrupted);
        } catch (Exception failure) {
            throw new IOException("gesture_injection_unconfirmed", failure);
        }
    }

    private void checkGestureConnection(LocalSocket client) throws IOException {
        StructPollfd descriptor = new StructPollfd();
        descriptor.fd = client.getFileDescriptor();
        descriptor.events = (short) (OsConstants.POLLHUP | OsConstants.POLLERR);
        try {
            Os.poll(new StructPollfd[]{descriptor}, 0);
            if ((descriptor.revents & (OsConstants.POLLHUP | OsConstants.POLLERR | OsConstants.POLLNVAL)) != 0) {
                throw new IOException("gesture_connection_closed");
            }
        } catch (ErrnoException failure) {
            throw new IOException("gesture_connection_unavailable", failure);
        }
    }

    private static void writeFrame(DataOutputStream output, int status, byte[] payload) throws IOException {
        output.writeByte(status);
        output.writeInt(payload == null ? 0 : payload.length);
        if (payload != null) {
            output.write(payload);
        }
        output.flush();
    }

    @SuppressWarnings("deprecation")
    private Capture capture(UiAutomation automation, boolean afterScroll) throws IOException {
        WindowSnapshot snapshot;
        if (adaptive) {
            snapshot = awaitQuietWindow(afterScroll);
        } else {
            // Older desktop clients still depend on the longer idle check after an action.
            try {
                automation.waitForIdle(100, 600);
            } catch (TimeoutException busy) {
                return MOVING;
            }
            synchronized (windowEvents) {
                snapshot = new WindowSnapshot(changeSequence, scrollSequence);
            }
        }
        if (snapshot == null) {
            return MOVING;
        }
        AccessibilityNodeInfo root = automation.getRootInActiveWindow();
        if (root == null) {
            return UNAVAILABLE;
        }
        try {
            CharSequence packageName = root.getPackageName();
            if (packageName == null) {
                return UNAVAILABLE;
            }
            if (!BOSS_PACKAGE.contentEquals(packageName)) {
                return FOREIGN;
            }
            WindowManager windows = getContext().getSystemService(WindowManager.class);
            Point screen = new Point();
            windows.getDefaultDisplay().getRealSize(screen);
            ByteArrayOutputStream bytes = new ByteArrayOutputStream(32768);
            OutputStream bounded = new OutputStream() {
                @Override
                public void write(int value) throws IOException {
                    checkSize(1);
                    bytes.write(value);
                }

                @Override
                public void write(byte[] data, int offset, int length) throws IOException {
                    checkSize(length);
                    bytes.write(data, offset, length);
                }

                private void checkSize(int length) throws IOException {
                    if (length > MAX_XML_BYTES - bytes.size()) {
                        throw new IOException("Hierarchy exceeds size limit");
                    }
                }
            };
            XmlSerializer xml = Xml.newSerializer();
            xml.setOutput(bounded, "UTF-8");
            xml.startDocument("UTF-8", true);
            xml.startTag(null, "hierarchy");
            xml.attribute(null, "rotation", Integer.toString(windows.getDefaultDisplay().getRotation()));
            xml.attribute(null, "window-id", Integer.toString(root.getWindowId()));
            xml.attribute(null, "change-sequence", Long.toString(snapshot.changes));
            xml.attribute(null, "scroll-sequence", Long.toString(snapshot.scrolls));
            xml.attribute(null, "touch-slop", Integer.toString(
                    ViewConfiguration.get(getContext()).getScaledTouchSlop()));
            if (!writeNode(xml, root, screen, 0, 0, snapshot.changes)) {
                return CHANGED;
            }
            xml.endTag(null, "hierarchy");
            xml.endDocument();
            xml.flush();
            synchronized (windowEvents) {
                if (snapshot.changes != changeSequence) {
                    return CHANGED;
                }
            }
            return new Capture(0, bytes.toByteArray());
        } finally {
            root.recycle();
        }
    }

    @SuppressWarnings("deprecation")
    private boolean writeNode(XmlSerializer xml, AccessibilityNodeInfo node, Point screen, int index,
                              int depth, long expectedGeneration) throws IOException {
        if (scrollAware && changeSequence != expectedGeneration) {
            return false;
        }
        if (depth >= MAX_TREE_DEPTH) {
            throw new IOException("Hierarchy exceeds depth limit");
        }
        xml.startTag(null, "node");
        xml.attribute(null, "index", Integer.toString(index));
        xml.attribute(null, "text", xmlText(node.getText()));
        xml.attribute(null, "resource-id", xmlText(node.getViewIdResourceName()));
        xml.attribute(null, "class", xmlText(node.getClassName()));
        xml.attribute(null, "package", xmlText(node.getPackageName()));
        xml.attribute(null, "content-desc", xmlText(node.getContentDescription()));
        xml.attribute(null, "checkable", Boolean.toString(node.isCheckable()));
        xml.attribute(null, "checked", Boolean.toString(node.isChecked()));
        xml.attribute(null, "clickable", Boolean.toString(node.isClickable()));
        xml.attribute(null, "enabled", Boolean.toString(node.isEnabled()));
        xml.attribute(null, "focusable", Boolean.toString(node.isFocusable()));
        xml.attribute(null, "focused", Boolean.toString(node.isFocused()));
        xml.attribute(null, "scrollable", Boolean.toString(node.isScrollable()));
        xml.attribute(null, "long-clickable", Boolean.toString(node.isLongClickable()));
        xml.attribute(null, "password", Boolean.toString(node.isPassword()));
        xml.attribute(null, "selected", Boolean.toString(node.isSelected()));
        Rect bounds = new Rect();
        node.getBoundsInScreen(bounds);
        if (!bounds.intersect(0, 0, screen.x, screen.y)) {
            bounds.setEmpty();
        }
        xml.attribute(null, "bounds", "[" + bounds.left + "," + bounds.top + "]["
                + bounds.right + "," + bounds.bottom + "]");
        for (int i = 0, count = node.getChildCount(); i < count; i++) {
            AccessibilityNodeInfo child = node.getChild(i);
            if (child == null) {
                continue;
            }
            try {
                if (child.isVisibleToUser() && !writeNode(xml, child, screen, i, depth + 1, expectedGeneration)) {
                    return false;
                }
            } finally {
                child.recycle();
            }
        }
        xml.endTag(null, "node");
        return true;
    }

    private static String xmlText(CharSequence value) {
        if (value == null) {
            return "";
        }
        StringBuilder text = new StringBuilder(value.length());
        for (int offset = 0; offset < value.length();) {
            int codePoint = Character.codePointAt(value, offset);
            offset += Character.charCount(codePoint);
            if (codePoint == 0x9 || codePoint == 0xA || codePoint == 0xD
                    || (codePoint >= 0x20 && codePoint <= 0xD7FF)
                    || (codePoint >= 0xE000 && codePoint <= 0xFFFD)
                    || (codePoint >= 0x10000 && codePoint <= 0x10FFFF)) {
                text.appendCodePoint(codePoint);
            }
        }
        return text.toString();
    }

    private void complete(int result) {
        if (ended.compareAndSet(false, true)) {
            handler.removeCallbacks(watchdog);
            finish(result, new Bundle());
        }
    }
}
