// ============================================================================
//  PlantLifeSupport — SENSOR POD enclosure           hardware/enclosures/pod/pod.scad
//
//  Parts in this file (choose with  -D 'part="name"'  or edit `part` below):
//    base        bottom half of the clamshell: PCB standoffs, connector windows,
//                drip louvres, stake socket, camera-arm lug
//    lid         top half: light window over the BH1750, two screw bosses
//    stake       printed cruciform stake with the soil-probe holder
//    cam_arm     adjustable arm (pan at the pod, tilt at the camera)
//    cam_cradle  clip that holds the thermal camera
//    assembly    everything together (preview only, not for printing)
//    print_all   every part laid flat in print orientation on one plate
//
//  Units are mm. Z is "up" in the assembled pod; the PCB lies flat inside.
//  (0,0) is the PCB's own corner, so all PCB-related numbers can be copied
//  straight from KiCad once hardware/pod/ exists.
//
//  Everything you are likely to touch is in the PARAMETERS section.
//  Walls are 3 mm, floors 2.4 mm; the PCB has 0.3 mm clearance all round.
// ============================================================================

part = "assembly";

// ---------------------------------------------------------------------------
//  PARAMETERS
// ---------------------------------------------------------------------------

/* [PCB — copy from hardware/pod/ when the board file exists] */
pcb_x       = 50;              // board width  (X). Connectors are on the y=0 edge.
pcb_y       = 70;              // board length (Y). Sensors are near the y=pcb_y edge.
pcb_t       = 1.6;             // board thickness
pcb_holes   = [[3, 3], [47, 67]];   // M2.5 mounting holes [x, y] from the board corner
pcb_clear   = 0.3;             // gap between board edge and enclosure wall

// Connector centres along the y=0 edge (X position on the board) and how far
// the receptacle sits above the PCB top (0 = soldered flat on the board,
// 1.6 = on a breakout board that is itself flat on the PCB).
usb_c_x     = 10;   usb_c_zoff = 1.6;
usb_a_x     = 24;   usb_a_zoff = 0;
usb_b_x     = 40;   usb_b_zoff = 0;

// Sensor positions (centre of the sensing element, board coordinates).
bh1750_pos  = [10, 62];        // light sensor -> gets the window in the lid
bme280_pos  = [35, 62];        // air sensor   -> near the louvred end wall

// Soil-probe lead: the 3-pin JST-XH plug is passed up through this slot in
// the floor. Position is the slot centre on the board, size fits the plug.
probe_slot_pos  = [8, 22];
probe_slot_size = [11, 6.5];

/* [Enclosure] */
wall        = 3.0;             // side wall thickness
floor_t     = 2.4;             // base floor thickness
top_t       = 2.4;             // lid top thickness
corner_r    = 3.0;             // outside corner radius
standoff_h  = 14;              // floor top -> PCB underside (room for the stake socket)
base_h      = 32;              // interior height of the base (floor top -> parting line)
lid_h       = 5;               // interior depth of the lid above the parting line
lip_h       = 3;               // how far the base lip reaches up into the lid
lip_t       = 1.4;             // thickness of that lip
lip_gap     = 0.2;             // clearance between lip and lid skirt

/* [Stake socket] */
dowel_d     = 10;              // wooden dowel diameter; the printed stake matches it
socket_gap  = 0.4;             // bore = dowel_d + socket_gap
socket_pos  = [25, 35];        // socket centre on the board (keep clear of the probe slot)
socket_od   = 16;              // outer diameter of the socket tube

/* [Printed stake] */
stake_len       = 200;         // length below the pod floor
stake_w         = 14;          // cruciform width across the blades
stake_t         = 3;           // blade thickness
spigot_len      = 13;          // round spigot that enters the socket
probe_w         = 23;          // capacitive soil sensor v1.2 PCB width
probe_t         = 1.6;         // ...and thickness
probe_holder_z0 = 20;          // probe top sits this far below the pod floor
probe_holder_len= 35;          // length of the rails that grip the probe

/* [Thermal camera + arm] */
cam_w = 28; cam_h = 28; cam_d = 12;   // camera body (w x h x thickness)
cam_clear   = 0.5;
cam_arm_len = 70;              // pod lug -> camera pivot
cam_lug_y   = 35;              // where the lug sits along the +X outer wall
cam_lug_h   = 22;              // lug height from the pod's underside

/* [Louvres (air vents)] */
louvre_pitch   = 4.0;          // vertical spacing between blades
louvre_t       = 1.2;          // blade thickness
louvre_protrude= 2.0;          // how far blade tips stick out past the wall (drip edge)
end_vent_x     = [8, 42];      // window extent along the sensor end wall (board X)
side_vent_y    = [50, 62];     // window extent along both long walls (board Y)

/* [Light window] */
light_hole_d   = 8;            // clear hole over the BH1750
light_disc_d   = 12;           // recess for a glued-on clear disc (PET/acrylic)
light_disc_t   = 1.0;

/* [Hardware] */
m25_clear      = 2.9;          // M2.5 clearance hole
m25_head_d     = 5.6;  m25_head_h = 1.2;   // pan-head counterbore in the floor
m25_insert_d   = 3.5;  m25_insert_depth = 5.5;   // M2.5 heat-set insert
m3_clear       = 3.4;
m3_insert_d    = 4.0;  m3_insert_depth  = 6.0;   // M3 heat-set insert

/* [Connector receptacles (shell size, w x h) — standard parts] */
usb_c_shell = [8.94, 3.26];
usb_a_shell = [13.2, 5.8];
usb_b_shell = [12.0, 10.9];
conn_clear  = 0.5;             // clearance around each receptacle shell

$fn = 48;

// ---------------------------------------------------------------------------
//  DERIVED VALUES (no need to edit)
// ---------------------------------------------------------------------------
ix = pcb_x + 2*pcb_clear;  iy = pcb_y + 2*pcb_clear;    // interior
ox = ix + 2*wall;          oy = iy + 2*wall;            // exterior
x0 = -pcb_clear;           y0 = -pcb_clear;             // interior corner
pcb_top  = standoff_h + pcb_t;
ceil_z   = base_h + lid_h;                               // underside of the lid plate
boss_d   = 7;
louvre_z = [pcb_top + 3, base_h - lip_h - 1];            // vent window height range
eps = 0.01;

module rbox(size, r) {   // box with rounded vertical edges
    hull() for (sx=[0,1], sy=[0,1])
        translate([r + sx*(size[0]-2*r), r + sy*(size[1]-2*r), 0]) cylinder(r=r, h=size[2]);
}

// ---------------------------------------------------------------------------
//  LOUVRE — a window in a wall with 45° blades sloping down and outward.
//  Local frame: wall inner face is the plane y=0, outside is +y,
//  window runs x in [0,span], z in [z0,z1].
// ---------------------------------------------------------------------------
module louvre_cut(span, z0, z1)
    translate([0, -1, z0]) cube([span, wall + 2, z1 - z0]);

module louvre_blades(span, z0, z1) {
    run = wall + louvre_protrude;                    // horizontal reach of one blade
    n   = ceil((z1 - z0 + 0.7*louvre_t - run) / louvre_pitch);
    intersection() {
        union() for (k = [0 : n])
            translate([-2, 0, z1 + 0.7*louvre_t - k*louvre_pitch])
                rotate([-45, 0, 0]) translate([0, 0, -louvre_t])
                    cube([span + 4, run*sqrt(2), louvre_t]);
        translate([-2, -0.5, z0]) cube([span + 4, run + 0.5, z1 - z0 + 2]);   // starts 0.5 inside the wall face so blades merge cleanly
    }
}

// Place a louvre (cut or blades) on one of the three vented walls.
module on_end_wall()   translate([end_vent_x[0], pcb_y + pcb_clear, 0]) children();
module on_right_wall() translate([pcb_x + pcb_clear, side_vent_y[1], 0]) rotate([0,0,-90]) children();
module on_left_wall()  translate([-pcb_clear, side_vent_y[0], 0]) rotate([0,0,90]) children();
end_span  = end_vent_x[1] - end_vent_x[0];
side_span = side_vent_y[1] - side_vent_y[0];

module all_louvre_cuts() {
    on_end_wall()   louvre_cut(end_span,  louvre_z[0], louvre_z[1]);
    on_right_wall() louvre_cut(side_span, louvre_z[0], louvre_z[1]);
    on_left_wall()  louvre_cut(side_span, louvre_z[0], louvre_z[1]);
}
module all_louvre_blades() {
    on_end_wall()   louvre_blades(end_span,  louvre_z[0], louvre_z[1]);
    on_right_wall() louvre_blades(side_span, louvre_z[0], louvre_z[1]);
    on_left_wall()  louvre_blades(side_span, louvre_z[0], louvre_z[1]);
}

// ---------------------------------------------------------------------------
//  BASE
// ---------------------------------------------------------------------------
module connector_window(cx, shell, zoff) {
    w = shell[0] + 2*conn_clear;
    translate([cx - w/2, y0 - wall - 1, pcb_top + zoff - conn_clear])
        cube([w, wall + 2, base_h]);          // open to the rim: PCB drops straight in
}

module cam_lug() {
    translate([pcb_x + pcb_clear - 0.5, cam_lug_y - 7, -floor_t])
        cube([12.5, 14, cam_lug_h]);
}

module pod_base() {
    difference() {
        union() {
            difference() {
                union() {
                    translate([x0 - wall, y0 - wall, -floor_t]) rbox([ox, oy, floor_t + base_h], corner_r);
                    cam_lug();
                }
                // cavity
                translate([x0, y0, 0]) cube([ix, iy, base_h + 1]);
                // lip: remove the outer part of the wall for the top lip_h
                translate([0, 0, base_h - lip_h]) difference() {
                    translate([x0 - wall - 1, y0 - wall - 1, 0]) cube([ox + 2, oy + 2, lip_h + 1]);
                    translate([x0 - lip_t, y0 - lip_t, -1]) rbox([ix + 2*lip_t, iy + 2*lip_t, lip_h + 3], 1);
                }
                connector_window(usb_c_x, usb_c_shell, usb_c_zoff);
                connector_window(usb_a_x, usb_a_shell, usb_a_zoff);
                connector_window(usb_b_x, usb_b_shell, usb_b_zoff);
                all_louvre_cuts();
            }
            all_louvre_blades();
            // PCB standoffs
            for (h = pcb_holes) translate([h[0], h[1], 0]) cylinder(d = boss_d, h = standoff_h);
            // stake socket tube + set-screw block
            translate([socket_pos[0], socket_pos[1], 0]) cylinder(d = socket_od, h = standoff_h - 3);
            translate([socket_pos[0] - 5, socket_pos[1] - socket_od/2 - 4, 0]) cube([10, 6, 10]);
        }
        // screw holes through standoffs and floor, counterbored from below
        for (h = pcb_holes) {
            translate([h[0], h[1], -floor_t - 1]) cylinder(d = m25_clear, h = standoff_h + floor_t + 2);
            translate([h[0], h[1], -floor_t - 1]) cylinder(d = m25_head_d, h = m25_head_h + 1);
        }
        // stake bore
        translate([socket_pos[0], socket_pos[1], -floor_t - 1]) cylinder(d = dowel_d + socket_gap, h = standoff_h + 2);
        // M3 set screw (heat-set insert, horizontal) into the bore
        translate([socket_pos[0], socket_pos[1] - socket_od/2 - 5, 5]) rotate([-90, 0, 0])
            cylinder(d = m3_insert_d, h = socket_od);
        // probe lead slot
        translate([probe_slot_pos[0] - probe_slot_size[0]/2, probe_slot_pos[1] - probe_slot_size[1]/2, -floor_t - 1])
            cube([probe_slot_size[0], probe_slot_size[1], floor_t + 2]);
        // weep hole near the connector end
        translate([pcb_x - 8, 10, -floor_t - 1]) cylinder(d = 2.5, h = floor_t + 2);
        // camera arm pivot insert, vertical, from the lug top
        translate([pcb_x + pcb_clear + 6, cam_lug_y, cam_lug_h - floor_t - m3_insert_depth]) cylinder(d = m3_insert_d, h = m3_insert_depth + 1);
    }
}

// ---------------------------------------------------------------------------
//  LID
// ---------------------------------------------------------------------------
module pod_lid() {
    difference() {
        translate([x0 - wall, y0 - wall, base_h - lip_h]) rbox([ox, oy, lid_h + lip_h + top_t], corner_r);
        // cavity
        translate([x0, y0, base_h - lip_h - 1]) cube([ix, iy, lid_h + lip_h + 1]);
        // pocket for the base lip
        translate([x0 - lip_t - lip_gap, y0 - lip_t - lip_gap, base_h - lip_h - 1])
            rbox([ix + 2*(lip_t + lip_gap), iy + 2*(lip_t + lip_gap), lip_h + 1], 1 + lip_gap);
        // light window over the BH1750 + recess for a clear disc
        translate([bh1750_pos[0], bh1750_pos[1], ceil_z - 1]) cylinder(d = light_hole_d, h = top_t + 2);
        translate([bh1750_pos[0], bh1750_pos[1], ceil_z + top_t - light_disc_t]) cylinder(d = light_disc_d, h = light_disc_t + 1);
    }
    // screw bosses: ceiling down to the PCB top, heat-set insert in the end
    difference() {
        for (h = pcb_holes) translate([h[0], h[1], pcb_top]) cylinder(d = boss_d, h = ceil_z - pcb_top + eps);
        for (h = pcb_holes) translate([h[0], h[1], pcb_top - 1]) cylinder(d = m25_insert_d, h = m25_insert_depth + 1);
    }
}

// ---------------------------------------------------------------------------
//  STAKE  (modelled along Z: z=0 at the pod underside, body goes down)
// ---------------------------------------------------------------------------
module stake() {
    pad_x0  = stake_t/2;                         // pad starts just past the crossing blade
    pad_w   = probe_w + 2*0.3 + 2*3;             // probe + gap + two 3 mm rails
    rail_h  = stake_t/2 + probe_t + 0.4 + 1.5;   // rail height above the blade centre line
    // spigot into the pod socket
    cylinder(d = dowel_d - 0.2, h = spigot_len);
    difference() {
        union() {
            // cruciform body
            for (a = [0, 90]) rotate([0, 0, a])
                translate([-stake_w/2, -stake_t/2, -stake_len]) cube([stake_w, stake_t, stake_len]);
            // probe pad: the +X blade widened over the holder length
            translate([pad_x0, -stake_t/2, -(probe_holder_z0 + probe_holder_len)])
                cube([pad_w, stake_t, probe_holder_len]);
            // two L-rails on the +Y face of the pad
            for (sx = [0, 1]) translate([pad_x0 + sx*(pad_w - 3), 0, -(probe_holder_z0 + probe_holder_len)]) {
                cube([3, rail_h, probe_holder_len]);                                        // post
                translate([sx ? -1.5 : 3, rail_h - 1.5, 0]) cube([1.5, 1.5, probe_holder_len]); // lip
            }
            // top stop: the probe is slid up from below until it hits this
            translate([pad_x0, 0, -probe_holder_z0 - 2]) cube([pad_w, rail_h, 2]);
        }
        // pointed tip (45° chisel on every blade)
        translate([0, 0, -stake_len - 1]) difference() {
            cylinder(r = stake_w, h = stake_w/2);
            cylinder(d1 = 2, d2 = stake_w + 2, h = stake_w/2);   // 45° taper: prints lying down without support
        }
    }
}

// Print orientation: lying along X, rolled 45° so it rests on two blade tips.
stake_trim = 1.5;   // flatten the resting blade tips and the spigot (keeps the spigot's underside within 45°)
module stake_print() {
    lowest = -(stake_w/2) * sqrt(2)/2;                  // blade tip after the 45° roll
    translate([0, 0, -(lowest + stake_trim)])
        intersection() {
            rotate([0, -90, 0]) rotate([0, 0, -45]) stake();
            translate([-spigot_len - 1, -50, lowest + stake_trim]) cube([stake_len + spigot_len + 2, 100, 100]);
        }
}

// ---------------------------------------------------------------------------
//  CAMERA CRADLE  (printed back-plate down; camera slides in from the -Y end)
// ---------------------------------------------------------------------------
cr_t = 2.5;  cr_pad_t = 5;  cr_pad_w = 12;
cw = cam_w + 2*cam_clear;  ch = cam_h + 2*cam_clear;  cd = cam_d + cam_clear;
cr_rail_h = cr_t + cd + 3;                 // rail height incl. the lip
cr_pivot_z = cr_t + cd/2;                  // tilt axis height above the back plate
cr_outer_w = cw + 2*cr_t + 2*cr_pad_t;     // overall width across the pivot pads

module cam_cradle() {
    difference() {
        union() {
            translate([-cw/2 - cr_t, -ch/2, 0]) cube([cw + 2*cr_t, ch + 3, cr_t]);  // back plate (+3 = end stop)
            for (s = [-1, 1]) {
                // rail
                translate([s > 0 ? cw/2 : -cw/2 - cr_t, -ch/2, 0]) cube([cr_t, ch, cr_rail_h]);
                // lip with a 45° underside, hanging over the camera's front edge
                translate([s > 0 ? cw/2 : -cw/2, 0, 0]) rotate([90, 0, 0]) mirror([s > 0 ? 1 : 0, 0, 0])
                    linear_extrude(height = ch, center = true)
                        polygon([[0, cr_rail_h - 3], [1.5, cr_rail_h - 1.5], [1.5, cr_rail_h - 1.2], [0.3, cr_rail_h], [0, cr_rail_h]]);
                // pivot pad (full height from the bed so it prints without support)
                translate([s > 0 ? cw/2 + cr_t : -cw/2 - cr_t - cr_pad_t, -cr_pad_w/2, 0]) cube([cr_pad_t, cr_pad_w, cr_t + cd]);
            }
            // end stop at the +Y end (camera top)
            translate([-cw/2 - cr_t, ch/2, 0]) cube([cw + 2*cr_t, 3, cr_rail_h - 3]);
            // small retention bump near the open end
            translate([-cw/2 + 4, -ch/2 + 2, cr_t - eps]) cube([cw - 8, 2, 0.5]);
        }
        // tilt-axis insert holes in both pads
        for (s = [-1, 1]) translate([s*(cr_outer_w/2 + 1), 0, cr_pivot_z]) rotate([0, -s*90, 0])
            cylinder(d = m3_insert_d, h = m3_insert_depth + 1);
    }
}

// ---------------------------------------------------------------------------
//  CAMERA ARM  (printed flat; pan screw at the root, tilt screws at the prongs)
// ---------------------------------------------------------------------------
arm_w = 12; arm_t = 5; prong_t = 3; prong_w = 12; prong_h = 24; arm_root_d = 16;
prong_gap = cr_outer_w + 2*0.3;            // inside distance between the prongs

module cam_arm() {
    difference() {
        union() {
            translate([0, -arm_w/2, 0]) cube([cam_arm_len, arm_w, arm_t]);
            cylinder(d = arm_root_d, h = arm_t);
            translate([cam_arm_len - prong_w, -prong_gap/2 - prong_t, 0]) cube([prong_w, prong_gap + 2*prong_t, arm_t]);
            for (s = [-1, 1]) translate([cam_arm_len - prong_w, s > 0 ? prong_gap/2 : -prong_gap/2 - prong_t, 0])
                cube([prong_w, prong_t, arm_t + prong_h]);
        }
        translate([0, 0, -1]) cylinder(d = m3_clear, h = arm_t + 2);                          // pan screw
        translate([cam_arm_len - prong_w/2, 0, arm_t + prong_h - prong_w/2]) rotate([90, 0, 0])
            cylinder(d = m3_clear, h = prong_gap + 2*prong_t + 2, center = true);               // tilt screws
    }
}

// ---------------------------------------------------------------------------
//  ASSEMBLY / PRINT PLATE
// ---------------------------------------------------------------------------
module pcb_dummy() color("green", 0.6) translate([0, 0, standoff_h]) difference() {
    cube([pcb_x, pcb_y, pcb_t]);
    for (h = pcb_holes) translate([h[0], h[1], -1]) cylinder(d = 2.7, h = 4);
}

module assembly() {
    color("DarkSeaGreen") pod_base();
    color("LightSteelBlue", 0.55) pod_lid();
    pcb_dummy();
    color("BurlyWood") translate([socket_pos[0], socket_pos[1], -floor_t]) stake();
    // arm on the lug, prongs up, camera tilted 30° down
    color("Gray") translate([pcb_x + pcb_clear + 6, cam_lug_y, cam_lug_h - floor_t]) cam_arm();
    color("DimGray") translate([pcb_x + pcb_clear + 6 + cam_arm_len - prong_w/2, cam_lug_y, cam_lug_h - floor_t + arm_t + prong_h - prong_w/2])
        rotate([0, 120, 0]) rotate([0, 0, 90]) translate([0, 0, -cr_pivot_z]) cam_cradle();
}

module print_all() {
    translate([0, 0, floor_t]) pod_base();
    translate([ox + 15, oy, ceil_z + top_t]) rotate([180, 0, 0]) pod_lid();
    translate([0, -40, 0]) stake_print();
    translate([0, oy + 20, 0]) cam_arm();
    translate([ox + 60, oy + 40, 0]) cam_cradle();
}

if (part == "base")       translate([0, 0, floor_t]) pod_base();
if (part == "lid")        translate([0, 0, ceil_z + top_t]) rotate([180, 0, 0]) pod_lid();
if (part == "stake")      stake_print();
if (part == "cam_arm")    cam_arm();
if (part == "cam_cradle") cam_cradle();
if (part == "assembly")   assembly();
if (part == "print_all")  print_all();
