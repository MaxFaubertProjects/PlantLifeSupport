// ============================================================================
//  PlantLifeSupport — BRAIN BOX enclosure       hardware/enclosures/brain/brain.scad
//
//  Houses a Raspberry Pi 5 + official Active Cooler + the HAT+ (65 x 56.5 mm)
//  on 19 mm standoffs, with a service bay along the back wall for the
//  panel-mount barrel jack, USB-A extension, two PG7 glands and the float lead.
//
//  Parts (choose with -D 'part="name"'):
//    box           the body, printed floor-down
//    lid           flat lid with vent slots, printed top-down
//    pump_bracket  lid-mounted cantilever cradle for a Kamoer NKP style pump
//    assembly      preview of everything
//    print_all     all parts in print orientation on one plate
//
//  Coordinates: (0,0,0) is the inside corner of the box at floor level.
//  X runs along the long wall (left -> Pi ports on the right),
//  Y runs from the front wall (Pi HDMI side) to the back wall (service bay).
//  Units mm. Walls 2.4 mm, PCB clearance >= 2 mm everywhere.
// ============================================================================

part = "assembly";

// ---------------------------------------------------------------------------
//  PARAMETERS
// ---------------------------------------------------------------------------

/* [Raspberry Pi 5] */
pi_size        = [85, 56];
pi_holes       = [[3.5, 3.5], [61.5, 3.5], [3.5, 52.5], [61.5, 52.5]];   // 58 x 49 pattern
pi_t           = 1.6;
pi_pos         = [20, 2];      // Pi board corner inside the box (leaves a 20 mm bay on the left)
pi_standoff    = 6;            // floor top -> Pi underside
// Side ports on the x=85 edge, centre Y and size [w, h] of each jack
pi_ports_one_window = true;    // true: one window for all three jacks (position-tolerant)
pi_eth_y       = 10.25;  pi_eth_size  = [16.0, 13.5];
pi_usb3_y      = 27.0;   pi_usb_size  = [13.3, 15.6];
pi_usb2_y      = 45.0;
// Front edge (y=0): centre X of USB-C power and the two micro-HDMI
pi_usbc_x      = 11.2;
pi_hdmi_x      = [25.8, 39.3];
// Camera ribbon slot in the left wall (centre Y, width) — ribbon runs under the HAT
ribbon_y       = 30;  ribbon_w = 24;  ribbon_z = 20;
// Active cooler footprint on the Pi (for the vent slots above and below it)
cooler_x       = [12, 60];  cooler_y = [10, 43];

/* [HAT+] */
hat_size       = [65, 56.5];   // same hole pattern as the Pi, same origin
hat_standoff   = 19;           // Pi top -> HAT underside (extra-tall header)
hat_t          = 1.6;
hat_headroom   = 22;           // tallest HAT part (TO-220s, caps, terminals)

/* [Box] */
wall           = 2.4;
floor_t        = 2.4;
lid_t          = 2.4;
corner_r       = 3;
bay_depth      = 25;           // service bay between the Pi/HAT and the back wall
left_bay       = 20;           // = pi_pos[0]; room for the SD card and the ribbon bend
port_gap       = 3;            // Pi ports edge -> inside of the right wall
post_size      = 8;            // square lid-screw posts in the corners
lid_lip_h      = 2.5;  lid_lip_t = 1.6;  lid_gap = 0.25;
vent_slot_w    = 3;    vent_slot_pitch = 6;
foot_d         = 10;           // recess for stick-on rubber feet
keyhole        = true;         // two keyholes in the floor for wall mounting
keyhole_spacing= 60;

/* [Back-wall panel items: X along the wall, Z above the floor] */
panel_z        = 25;
barrel_x       = 16;   barrel_hole_d = 8.0;   // 2.1 mm panel-mount barrel jack (nut needs 11.5 mm clear)
usba_x         = 42;   usba_cut = [16, 8];  usba_hole_spacing = 25;  usba_hole_d = 3.2;   // panel-mount USB-A extension
gland_x        = [68, 90];  gland_hole_d = 12.5;  gland_nut_d = 20;     // PG7 glands
float_slot_pos = [16, 42];  float_slot = [6, 3];                        // 2-wire float lead

/* [Pump bracket (Kamoer NKP style, motor axis parallel to the back wall)] */
pump_motor_d   = 27.5;
pump_len       = 60;           // overall pump length (information only; sets preview)
pump_clear     = 0.5;
cradle_len     = 30;           // length of pump supported (motor section)
bracket_overhang = 45;         // lid edge -> near side of the cradle
bracket_t      = 5;            // arm plate thickness
bracket_w      = 34;           // arm / cradle width
bracket_screw_spacing = 30;    // two M3 heat-set inserts in the lid
bracket_x      = 54;           // centre of the bracket base along X on the lid
bracket_base_y = 62;           // where the bracket base starts on the lid (Y)
tie_slot       = [5, 2.5];     // zip-tie tunnels under the pump

/* [Hardware] */
m25_insert_d = 3.5;  m25_insert_depth = 5;
m3_clear     = 3.4;  m3_head_d = 6.2;  m3_head_h = 2.2;
m3_insert_d  = 4.0;  m3_insert_depth  = 6;

$fn = 48;

// ---------------------------------------------------------------------------
//  DERIVED
// ---------------------------------------------------------------------------
ix = pi_pos[0] + pi_size[0] + port_gap;                 // interior length
iy = pi_pos[1] + pi_size[1] + bay_depth;                // interior width
iz = pi_standoff + pi_t + hat_standoff + hat_t + hat_headroom;   // interior height
ox = ix + 2*wall;  oy = iy + 2*wall;
pi_z   = pi_standoff;            // Pi underside
pi_top = pi_z + pi_t;
hat_z  = pi_top + hat_standoff;
posts  = [[0, 0], [0, iy - post_size], [ix - post_size, iy - post_size], [ix - post_size, pi_pos[1] + pi_size[1] + 1]];
eps = 0.01;

module rbox(size, r) hull() for (sx=[0,1], sy=[0,1])
    translate([r + sx*(size[0]-2*r), r + sy*(size[1]-2*r), 0]) cylinder(r=r, h=size[2]);

function px(x) = pi_pos[0] + x;      // Pi board coords -> box coords
function py(y) = pi_pos[1] + y;

// vertical slots (through a horizontal plate) over a rectangle, running along Y
module slot_array(x_range, y_range, z, h) {
    n = floor((x_range[1] - x_range[0] - vent_slot_w) / vent_slot_pitch);
    for (i = [0 : n]) translate([x_range[0] + i*vent_slot_pitch, y_range[0], z])
        cube([vent_slot_w, y_range[1] - y_range[0], h]);
}

// ---------------------------------------------------------------------------
//  BOX
// ---------------------------------------------------------------------------
module wall_cut_x(x, z0, w, h) translate([x - w/2, -wall - 1, z0]) cube([w, wall + 2, h]);          // front wall (y=0)
module wall_cut_back(x, z0, w, h) translate([x - w/2, iy - 1, z0]) cube([w, wall + 2, h]);          // back wall
module wall_cut_right(y, z0, w, h) translate([ix - 1, y - w/2, z0]) cube([wall + 2, w, h]);         // right wall
module wall_cut_left(y, z0, w, h) translate([-wall - 1, y - w/2, z0]) cube([wall + 2, w, h]);       // left wall
// round hole through the back wall; holes over 6 mm get a 45° teardrop top so they print cleanly
module back_hole(x, z, d) translate([x, iy - 1, z]) rotate([-90, 0, 0]) {
    cylinder(d = d, h = wall + 2);
    if (d > 6) rotate([0, 0, 45]) translate([-d/2/sqrt(2), -d/2/sqrt(2), 0]) cube([d/sqrt(2), d/sqrt(2), wall + 2]);
}

module brain_box() {
    difference() {
        union() {
            difference() {
                translate([-wall, -wall, -floor_t]) rbox([ox, oy, floor_t + iz], corner_r);
                translate([0, 0, 0]) cube([ix, iy, iz + 1]);                       // cavity
            }
            for (p = posts) translate([p[0], p[1], 0]) cube([post_size, post_size, iz]);   // lid posts
            for (h = pi_holes) translate([px(h[0]), py(h[1]), 0]) cylinder(d = 6.5, h = pi_standoff);  // Pi posts
        }
        // lid-post inserts
        for (p = posts) translate([p[0] + post_size/2, p[1] + post_size/2, iz - m3_insert_depth]) cylinder(d = m3_insert_d, h = m3_insert_depth + 1);
        // Pi post inserts (M2.5)
        for (h = pi_holes) translate([px(h[0]), py(h[1]), pi_standoff - m25_insert_depth]) cylinder(d = m25_insert_d, h = m25_insert_depth + 1);

        // --- right wall: Pi side ports
        if (pi_ports_one_window)
            wall_cut_right(py(pi_size[1]/2), pi_top - 1, pi_size[1] - 2, pi_usb_size[1] + 2);
        else {
            wall_cut_right(py(pi_eth_y),  pi_top - 1, pi_eth_size[0] + 2, pi_eth_size[1] + 2);
            wall_cut_right(py(pi_usb3_y), pi_top - 1, pi_usb_size[0] + 2, pi_usb_size[1] + 2);
            wall_cut_right(py(pi_usb2_y), pi_top - 1, pi_usb_size[0] + 2, pi_usb_size[1] + 2);
        }
        // --- front wall: USB-C (accessible, not used for power) and the two micro-HDMI plugs
        wall_cut_x(px(pi_usbc_x), pi_top - 1, 12, 7.5);
        for (x = pi_hdmi_x) wall_cut_x(px(x), pi_top - 1, 12, 9);
        // --- left wall: camera ribbon slot (ribbon runs under the HAT)
        wall_cut_left(py(ribbon_y), ribbon_z, ribbon_w, 3);
        // --- back wall: panel items
        back_hole(barrel_x, panel_z, barrel_hole_d);
        wall_cut_back(usba_x, panel_z - usba_cut[1]/2, usba_cut[0], usba_cut[1]);
        for (s = [-1, 1]) back_hole(usba_x + s*usba_hole_spacing/2, panel_z, usba_hole_d);
        for (x = gland_x) back_hole(x, panel_z, gland_hole_d);
        wall_cut_back(float_slot_pos[0], float_slot_pos[1] - float_slot[1]/2, float_slot[0], float_slot[1]);
        // --- back wall: low intake slots (cool air in, under the HAT)
        for (x = [26 : 16 : ix - 24]) wall_cut_back(x, 4, 12, 3);
        // --- floor: intake slots under the cooler
        slot_array([px(cooler_x[0]), px(cooler_x[1])], [py(cooler_y[0]), py(cooler_y[1])], -floor_t - 1, floor_t + 2);
        // --- floor: feet recesses and keyholes
        for (sx = [0, 1], sy = [0, 1]) translate([10 + sx*(ix - 20), 10 + sy*(iy - 20), -floor_t - 1]) cylinder(d = foot_d, h = 1.8);
        if (keyhole) for (s = [-1, 1]) translate([ix/2 + s*keyhole_spacing/2, iy/2, -floor_t - 1]) {
            cylinder(d = 8, h = floor_t + 2);
            translate([-2, 0, 0]) cube([4, 9, floor_t + 2]);
        }
    }
}

// ---------------------------------------------------------------------------
//  LID  (modelled in place: plate sits on top of the walls at z = iz)
// ---------------------------------------------------------------------------
module brain_lid() {
    difference() {
        union() {
            translate([-wall, -wall, iz]) rbox([ox, oy, lid_t], corner_r);
            // locating lip inside the walls
            translate([0, 0, iz - lid_lip_h]) difference() {
                translate([lid_gap, lid_gap, 0]) cube([ix - 2*lid_gap, iy - 2*lid_gap, lid_lip_h + eps]);
                translate([lid_gap + lid_lip_t, lid_gap + lid_lip_t, -1]) cube([ix - 2*(lid_gap + lid_lip_t), iy - 2*(lid_gap + lid_lip_t), lid_lip_h + 2]);
                for (p = posts) translate([p[0] - 0.3, p[1] - 0.3, -1]) cube([post_size + 0.6, post_size + 0.6, lid_lip_h + 2]);
            }
            // bosses for the pump-bracket inserts
            for (s = [-1, 1]) translate([bracket_x + s*bracket_screw_spacing/2, bracket_base_y + 12, iz - m3_insert_depth - 0.5]) cylinder(d = 8, h = m3_insert_depth + 0.5 + eps);
        }
        // lid screws
        for (p = posts) translate([p[0] + post_size/2, p[1] + post_size/2, iz - 1]) {
            cylinder(d = m3_clear, h = lid_t + 2);
            translate([0, 0, 1 + lid_t - m3_head_h]) cylinder(d = m3_head_d, h = m3_head_h + 1);
        }
        // exhaust slots over the cooler
        slot_array([px(cooler_x[0]), px(cooler_x[1])], [py(cooler_y[0]), py(cooler_y[1])], iz - 1, lid_t + 2);
        // pump bracket inserts
        for (s = [-1, 1]) translate([bracket_x + s*bracket_screw_spacing/2, bracket_base_y + 12, iz - m3_insert_depth - 1]) cylinder(d = m3_insert_d, h = m3_insert_depth + 1);
    }
}

// ---------------------------------------------------------------------------
//  PUMP BRACKET  (printed flat, base plate on the bed)
//  Local frame: base plate at the origin; +Y goes out over the reservoir.
// ---------------------------------------------------------------------------
base_len    = 24;                                   // bracket base plate along Y
cradle_h    = pump_motor_d/2 + pump_clear + 4;      // trough depth + 4 mm floor
cradle_w    = pump_motor_d + 2*pump_clear + 6;      // cradle width across the trough
cradle_y0   = (iy + wall - bracket_base_y) + bracket_overhang;   // bracket origin -> near side of the cradle

module pump_bracket() {
    cw = cradle_w;
    difference() {
        union() {
            translate([-bracket_screw_spacing/2 - 8, 0, 0]) cube([bracket_screw_spacing + 16, base_len, bracket_t]);   // base
            translate([-bracket_w/2, 0, 0]) cube([bracket_w, cradle_y0 + cradle_w, bracket_t]);                          // arm
            for (s = [-1, 1]) translate([s*(bracket_w/2 - 1.5) - 1.5, base_len - 1, 0]) cube([3, cradle_y0 - base_len + 1, bracket_t + 6]);  // stiffening ribs
            translate([-cradle_len/2, cradle_y0, 0]) cube([cradle_len, cw, bracket_t + cradle_h]);                     // cradle block
        }
        // trough (half-round, centre on the top face so there is no overhang)
        translate([-cradle_len/2 - 1, cradle_y0 + cw/2, bracket_t + cradle_h]) rotate([0, 90, 0]) cylinder(d = pump_motor_d + 2*pump_clear, h = cradle_len + 2);
        // zip-tie tunnels across the cradle, under the pump
        for (x = [-cradle_len/2 + 6, cradle_len/2 - 6 - tie_slot[0]]) translate([x, cradle_y0 - 1, bracket_t + 1]) cube([tie_slot[0], cw + 2, tie_slot[1]]);
        // screws into the lid inserts, counterbored
        for (s = [-1, 1]) translate([s*bracket_screw_spacing/2, 12, -1]) {
            cylinder(d = m3_clear, h = bracket_t + 2);
            translate([0, 0, 1 + bracket_t - m3_head_h]) cylinder(d = m3_head_d, h = m3_head_h + 1);
        }
    }
}

// ---------------------------------------------------------------------------
//  ASSEMBLY / PRINT PLATE
// ---------------------------------------------------------------------------
module pi_dummy() {
    color("green", 0.7) translate([px(0), py(0), pi_z]) cube([pi_size[0], pi_size[1], pi_t]);
    color("silver", 0.7) translate([px(cooler_x[0]), py(cooler_y[0]), pi_top]) cube([cooler_x[1] - cooler_x[0], cooler_y[1] - cooler_y[0], 17]);
    color("darkgreen", 0.7) translate([px(0), py(0), hat_z]) cube([hat_size[0], hat_size[1], hat_t]);
}

module assembly() {
    color("Tan") brain_box();
    color("LightSteelBlue", 0.5) brain_lid();
    pi_dummy();
    translate([bracket_x, bracket_base_y, iz + lid_t]) {
        color("SlateGray") pump_bracket();
        color("DimGray", 0.8) translate([-pump_len/2 + 10, cradle_y0 + cradle_w/2, bracket_t + cradle_h]) rotate([0, 90, 0]) cylinder(d = pump_motor_d, h = pump_len);
    }
}

module print_all() {
    translate([0, 0, floor_t]) brain_box();
    translate([ox + 15, oy, iz + lid_t]) rotate([180, 0, 0]) brain_lid();
    translate([ox + 60, -20, 0]) pump_bracket();
}

if (part == "box")          translate([0, 0, floor_t]) brain_box();
if (part == "lid")          translate([0, 0, iz + lid_t]) rotate([180, 0, 0]) brain_lid();
if (part == "pump_bracket") pump_bracket();
if (part == "assembly")     assembly();
if (part == "print_all")    print_all();
