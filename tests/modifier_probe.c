/* A minimal X client that writes down the modifier state every event carries.
 *
 * Fusion lives on Xwayland, and the question this answers is a narrow one:
 * when the virtual device holds shift and presses the middle button, does the
 * ButtonPress that reaches an X client carry ShiftMask? xev would do the job,
 * but it is not installed everywhere and this is shorter than its man page.
 *
 * Build and run (tests/live_modifier.py does both):
 *
 *     cc -O2 -o modifier_probe modifier_probe.c -lX11
 *     ./modifier_probe 6            # seconds to watch, then exit
 *
 * It prints one line per event, with the state mask in hex, and a summary
 * line at the end. Motion is counted rather than printed, except when its
 * state mask changes, which is the interesting part.
 */

#include <X11/Xlib.h>
#include <X11/Xutil.h>
#include <poll.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <time.h>
#include <unistd.h>

static double now_ms(void)
{
    struct timespec ts;
    clock_gettime(CLOCK_MONOTONIC, &ts);
    return ts.tv_sec * 1000.0 + ts.tv_nsec / 1e6;
}

static const char *shift_note(unsigned int state)
{
    return (state & ShiftMask) ? "  SHIFT" : "";
}

int main(int argc, char **argv)
{
    double seconds = argc > 1 ? atof(argv[1]) : 6.0;
    Display *dpy = XOpenDisplay(NULL);
    if (!dpy) {
        fprintf(stderr, "cannot open display %s\n", getenv("DISPLAY"));
        return 1;
    }

    int screen = DefaultScreen(dpy);
    Window root = RootWindow(dpy, screen);

    XSetWindowAttributes attrs;
    memset(&attrs, 0, sizeof(attrs));
    attrs.background_pixel = BlackPixel(dpy, screen);
    attrs.event_mask = KeyPressMask | KeyReleaseMask | ButtonPressMask |
                       ButtonReleaseMask | PointerMotionMask |
                       StructureNotifyMask | FocusChangeMask | ExposureMask;

    Window win = XCreateWindow(dpy, root, 0, 0, 640, 400, 0, CopyFromParent,
                               InputOutput, CopyFromParent,
                               CWBackPixel | CWEventMask, &attrs);
    XStoreName(dpy, win, "spacemouse modifier probe");
    XClassHint hint;
    hint.res_name = (char *)"spacemouse-probe";
    hint.res_class = (char *)"spacemouse-probe";
    XSetClassHint(dpy, win, &hint);
    XMapWindow(dpy, win);
    XFlush(dpy);

    printf("ready pid=%d window=0x%lx\n", (int)getpid(), (unsigned long)win);
    fflush(stdout);

    double start = now_ms();
    long motions = 0;
    unsigned int motion_state = 0;
    int have_motion_state = 0;
    long buttons = 0;
    long shifted_buttons = 0;
    int fd = ConnectionNumber(dpy);

    while (now_ms() - start < seconds * 1000.0) {
        while (XPending(dpy)) {
            XEvent ev;
            XNextEvent(dpy, &ev);
            double t = now_ms() - start;
            switch (ev.type) {
            case KeyPress:
            case KeyRelease: {
                KeySym sym = XLookupKeysym(&ev.xkey, 0);
                const char *name = XKeysymToString(sym);
                printf("%8.1f %-13s keycode=%-3u state=0x%04x sym=%s%s\n", t,
                       ev.type == KeyPress ? "KeyPress" : "KeyRelease",
                       ev.xkey.keycode, ev.xkey.state, name ? name : "?",
                       shift_note(ev.xkey.state));
                break;
            }
            case ButtonPress:
            case ButtonRelease:
                if (ev.type == ButtonPress) {
                    buttons++;
                    if (ev.xbutton.state & ShiftMask)
                        shifted_buttons++;
                }
                printf("%8.1f %-13s button=%-3u state=0x%04x%s\n", t,
                       ev.type == ButtonPress ? "ButtonPress" : "ButtonRelease",
                       ev.xbutton.button, ev.xbutton.state,
                       shift_note(ev.xbutton.state));
                break;
            case MotionNotify:
                motions++;
                if (!have_motion_state || ev.xmotion.state != motion_state) {
                    printf("%8.1f %-13s state=0x%04x%s\n", t, "MotionNotify",
                           ev.xmotion.state, shift_note(ev.xmotion.state));
                    motion_state = ev.xmotion.state;
                    have_motion_state = 1;
                }
                break;
            case FocusIn:
            case FocusOut:
                printf("%8.1f %-13s mode=%d detail=%d\n", t,
                       ev.type == FocusIn ? "FocusIn" : "FocusOut",
                       ev.xfocus.mode, ev.xfocus.detail);
                break;
            default:
                break;
            }
            fflush(stdout);
        }
        struct pollfd pfd;
        pfd.fd = fd;
        pfd.events = POLLIN;
        poll(&pfd, 1, 50);
    }

    printf("summary motion=%ld buttonpress=%ld with_shift=%ld\n", motions,
           buttons, shifted_buttons);
    fflush(stdout);
    XDestroyWindow(dpy, win);
    XCloseDisplay(dpy);
    return 0;
}
