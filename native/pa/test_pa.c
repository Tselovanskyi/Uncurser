// Focused host-side checks for PA timing and bounded native ownership.
#include <math.h>
#include <stdlib.h>

static int allocations, fail_after = -1;
static void *tracked_malloc(size_t size)
{
    if (!fail_after)
        return NULL;
    if (fail_after > 0)
        --fail_after;
    void *result = malloc(size);
    allocations += result != NULL;
    return result;
}
static void tracked_free(void *value)
{
    allocations -= value != NULL;
    free(value);
}
#define malloc tracked_malloc
#define free tracked_free
#include "kin_extruder.c"
#undef malloc
#undef free

#define CHECK(value, code) do { if (!(value)) return code; } while (0)
#define NEAR(value, expected) (fabs((value) - (expected)) < 1.e-9)

int UNCURSER_EXPORT
uncurser_pa_native_test(void)
{
    fail_after = 0;
    CHECK(uncurser_pa_alloc() == NULL && !allocations, 1);
    fail_after = 1;
    CHECK(uncurser_pa_alloc() == NULL && !allocations, 2);
    fail_after = -1;
    struct stepper_kinematics *sk = uncurser_pa_alloc();
    CHECK(sk != NULL && allocations == 2, 3);
    struct list_head moves;
    struct move m[8] = {0};
    list_init(&moves);
    for (int i = 0; i < 8; ++i) {
        m[i].print_time = i;
        m[i].move_t = 1.;
        m[i].start_v = 2.;
        m[i].start_pos.x = i * 2.;
        m[i].axes_r.y = 1.;
        list_add_tail(&m[i].node, &moves);
    }
    CHECK(!uncurser_pa_set(sk, 0., .1, .04), 4);
    CHECK(NEAR(sk->calc_position_cb(sk, &m[2], .5), 5.2), 5);
    CHECK(!uncurser_pa_set(sk, 3., .2, .04), 6);
    // A queued new value must not change earlier moves.
    CHECK(NEAR(sk->calc_position_cb(sk, &m[2], .5), 5.2), 7);
    CHECK(NEAR(sk->calc_position_cb(sk, &m[3], .5), 7.4), 8);
    CHECK(NEAR(sk->calc_position_cb(sk, &m[3], 0.), 6.3), 9);
    // Travel/non-PA moves retain their nominal position.
    m[4].axes_r.y = 0.;
    CHECK(NEAR(sk->calc_position_cb(sk, &m[4], .5), 9.), 10);
    fail_after = 0;
    CHECK(uncurser_pa_set(sk, 4., .3, .04) == -1, 11);
    CHECK(NEAR(sk->calc_position_cb(sk, &m[3], .5), 7.4), 12);
    CHECK(!uncurser_pa_set(sk, 4., .2, .04), 13);
    fail_after = -1;
    CHECK(!uncurser_pa_set(sk, 0., 0., 0.), 14);
    CHECK(NEAR(sk->calc_position_cb(sk, &m[2], .5), 5.), 15);
    CHECK(!uncurser_pa_set(sk, 0., .15, .04), 16);
    CHECK(NEAR(sk->calc_position_cb(sk, &m[5], .5), 11.3), 17);
    // Retired history must be freed as the step-generation horizon advances.
    for (int i = 0; i < 10000; ++i) {
        sk->last_flush_time = 10. + i * .1;
        CHECK(!uncurser_pa_set(sk, sk->last_flush_time, .1 + .01 * (i & 1), .04), 18);
        CHECK(allocations <= 6, 19);
    }
    uncurser_pa_free(sk);
    CHECK(!allocations, 20);
    return 0;
}
