/* SPDX-License-Identifier: MIT
 *
 * libpista_ae_guard.so — Zero-flicker AE ownership guard for pista-camera
 *
 * libcamera v0.7.2 IPASoftSimple unconditionally emits setSensorControls
 * (V4L2_CID_EXPOSURE + V4L2_CID_ANALOGUE_GAIN via VIDIOC_S_EXT_CTRLS) on
 * every frame even when Agc is omitted from the tuning YAML, causing
 * DelayedControls to overwrite external Sony-SMIA AE & HDR bracketing on SOF
 * and produce bright/dark flicker.
 *
 * When PISTA_AE_GUARD=1 is set inside pista-camera, this tiny LD_PRELOAD shim
 * filters ONLY V4L2_CID_EXPOSURE and V4L2_CID_ANALOGUE_GAIN from libcamera's
 * VIDIOC_S_EXT_CTRLS calls while passing all other controls (VBLANK, focus,
 * formats) and pista-camera's own VIDIOC_S_CTRL calls straight through.
 */
#define _GNU_SOURCE
#include <dlfcn.h>
#include <linux/videodev2.h>
#include <stdarg.h>
#include <stdlib.h>
#include <string.h>

typedef int (*real_ioctl_fn)(int, unsigned long, ...);

static int is_ae_ctrl(__u32 id) {
    return id == V4L2_CID_EXPOSURE || id == V4L2_CID_ANALOGUE_GAIN;
}

int ioctl(int fd, unsigned long request, ...) {
    static real_ioctl_fn real_ioctl = NULL;
    if (!real_ioctl) {
        real_ioctl = (real_ioctl_fn)dlsym(RTLD_NEXT, "ioctl");
    }

    va_list ap;
    va_start(ap, request);
    void *arg = va_arg(ap, void *);
    va_end(ap);

    if (request == VIDIOC_S_EXT_CTRLS && arg != NULL) {
        const char *en = getenv("PISTA_AE_GUARD");
        if (en && en[0] == '1') {
            struct v4l2_ext_controls *ec = (struct v4l2_ext_controls *)arg;
            if (ec->count > 0 && ec->controls != NULL && ec->count <= 32) {
                struct v4l2_ext_control kept[32];
                __u32 n_kept = 0;
                int dropped = 0;
                for (__u32 i = 0; i < ec->count; i++) {
                    if (is_ae_ctrl(ec->controls[i].id)) {
                        dropped = 1;
                    } else {
                        kept[n_kept++] = ec->controls[i];
                    }
                }
                if (dropped) {
                    if (n_kept == 0) {
                        return 0;
                    }
                    struct v4l2_ext_controls sub = *ec;
                    sub.count = n_kept;
                    sub.controls = kept;
                    return real_ioctl(fd, request, &sub);
                }
            }
        }
    }

    return real_ioctl(fd, request, arg);
}
