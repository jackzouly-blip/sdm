*KEYWORD
$ Inflator template (extracted from 6P_CAB_update2.k):
$   curves 10/12/13/14 + AIRBAG_HYBRID_ID. Reference geometry lives in
$   the mesh include (generated per bag by vektor3d).
$ A23=0 (no vent): vent part 74700001 belongs to the 6P CAB model and
$   does not exist in the bag-only model. Once the real vent area is
$   known, set it at AIRBAG card line 4 field 3 (positive area).
$> Include file: F:\00_word\CAB_0729\test\11111\6P_CAB_update2.k
$> (Used by master file: F:\00_word\CAB_0729\test\11111\main.key)
$> 
$
$
$ ============
$ DEFINE cards
$ ============
$
$
*DEFINE_CURVE
$:
$: Cross-reference summary for Load-curve 10
$: -----------------------------------------------
$:
$: Control vol (airbag) 1 : Vent orifice coefficient vs relative pressure
$: X axis : Relative pressure          (Units: Scalar, no units)
$: Y axis : Vent orifice coefficient   (Units: Scalar, no units)
$:
$: Usage: Transient analysis
$:
        10         0       0.0       0.0       0.0       0.0         0
                 0.5                 1.2
          0.69999999          0.64499998
          0.89999998          0.63499999
                 1.0                 0.1
$
$
*DEFINE_CURVE
$:
$: Cross-reference summary for Load-curve 12
$: -----------------------------------------------
$:
$: Control vol (airbag) 1 : Inflator mass flow rate vs time
$: X axis : Time                       (Units: Time)
$: Y axis : Inflator mass flow rate    (Units: Mass / time)
$:
$: Usage: Transient analysis
$:
        12         0       1.0       1.0       0.0       0.0         0
                 0.0                 0.0
                 0.1                 0.0
                 0.2                 0.0
          0.30000001                 0.0
          0.40000001                 0.0
                 0.5                 0.0
          0.60000002                 0.0
          0.69999999                 0.0
          0.80000001                 0.0
          0.89999998                 0.0
                 1.0                 0.0
                 1.1                 0.0
                 1.2                 0.0
                 1.3                 0.0
                 1.4                 0.0
                 1.5                 0.0
                 1.6                 0.0
                 1.7                 0.0
                 1.8                 0.0
                 1.9                 0.0
                 2.0                 0.0
           2.0999999                 0.0
                 2.2                 0.0
                 2.3                 0.0
           2.4000001                 0.0
                 2.5                 0.0
           2.5999999                 0.0
                 2.7                 0.0
                 2.8                 0.0
           2.9000001                 0.0
                 3.0                 0.0
           3.0999999                 0.0
                 3.2                 0.0
                 3.3                 0.0
           3.4000001                 0.0
                 3.5                 0.0
           3.5999999                 0.0
                 3.7                 0.0
                 3.8                 0.0
           3.9000001                 0.0
                 4.0                 0.0
           4.0999999                 0.0
           4.1999998                 0.0
           4.3000002            0.001288
           4.4000001            0.001287
                 4.5            0.001284
           4.5999999             0.00128
           4.6999998            0.001275
           4.8000002            0.001268
           4.9000001        0.0012610001
                 5.0            0.001252
           5.0999999            0.001243
           5.1999998            0.001234
           5.3000002            0.001224
           5.4000001            0.001214
                 5.5            0.001204
           5.5999999            0.001195
           5.6999998            0.001186
           5.8000002            0.001178
           5.9000001        0.0011699999
                 6.0            0.001164
           6.0999999            0.001158
           6.1999998        0.0011540001
           6.3000002             0.00115
           6.4000001            0.001148
                 6.5            0.001147
           6.5999999            0.001146
           6.6999998            0.001147
           6.8000002            0.001147
           6.9000001            0.001148
                 7.0            0.001149
           7.0999999             0.00115
           7.1999998             0.00115
           7.3000002             0.00115
           7.4000001            0.001149
                 7.5            0.001147
           7.5999999            0.001144
           7.6999998        0.0011399999
           7.8000002            0.001135
           7.9000001             0.00113
                 8.0        0.0011239999
           8.1000004            0.001118
           8.1999998            0.001111
           8.3000002            0.001104
           8.3999996            0.001096
                 8.5            0.001089
           8.6000004            0.001081
           8.6999998            0.001074
           8.8000002            0.001068
           8.8999996        0.0010620001
                 9.0            0.001057
           9.1000004            0.001053
           9.1999998        0.0010479999
           9.3000002            0.001044
           9.3999996             0.00104
                 9.5            0.001037
           9.6000004        0.0010319999
           9.6999998            0.001028
           9.8000002            0.001023
           9.8999996            0.001018
                10.0            0.001012
                11.0             9.33E-4
                12.0        8.9099997E-4
                13.0             8.64E-4
                14.0        8.2800002E-4
                15.0        8.2399999E-4
                16.0        8.0400001E-4
                17.0        8.2100002E-4
                18.0             7.88E-4
                19.0             7.72E-4
                20.0             7.26E-4
                21.0        7.0099998E-4
                22.0        6.7500002E-4
                23.0        6.5399997E-4
                24.0        6.3999998E-4
                25.0        6.0899998E-4
                26.0        6.0799997E-4
                27.0        5.6800002E-4
                28.0        5.7500001E-4
                29.0        5.3000002E-4
                30.0        5.1300001E-4
                33.0        4.5799999E-4
                36.0             4.01E-4
                39.0             2.98E-4
                42.0             2.87E-4
                45.0             2.25E-4
                48.0        1.9599999E-4
                51.0              1.6E-4
                54.0             1.16E-4
                57.0        7.5999997E-5
                60.0        9.9999997E-6
                63.0                 0.0
               200.0                 0.0
$
$
*DEFINE_CURVE
$:
$: Cross-reference summary for Load-curve 13
$: -----------------------------------------------
$:
$: Control vol (airbag) 1 : Inflator mass flow rate vs time
$: X axis : Time                       (Units: Time)
$: Y axis : Molar fraction of gas component  (Units: Scalar, no units)
$:
$: Usage: Transient analysis
$:
        13         0       0.0       0.0       0.0       0.0         0
                 0.0               0.317
               200.0               0.317
$
$
*DEFINE_CURVE
$:
$: Cross-reference summary for Load-curve 14
$: -----------------------------------------------
$:
$: Control vol (airbag) 1 : Inflator gas temperature vs time
$: X axis : Time                       (Units: Time)
$: Y axis : Inflator gas temperature   (Units: Temperature)
$:
$: Usage: Transient analysis
$:
        14         0       0.0       0.0       0.0       0.0         0
                 0.0               790.0
             10000.0               790.0
$
$
$
$ ============
$ AIRBAG cards
$ ============
$
*AIRBAG_HYBRID_ID
         1                                                                      
         1         3         0       1.0       1.0       0.0       0.0       0.0
     296.0 1.0125E-4   1.19E-9     8.314       1.0       0.0
       0.0       -10       0.0         0       0.0         0       0.0         0
         8       0.0         2         0        12         0
         0         0       0.0     0.029       1.0      26.6    0.0075       0.0
       0.0
        13        14       0.0     0.028       0.0    26.092    0.0082 -1.976E-6
       0.0
*END
