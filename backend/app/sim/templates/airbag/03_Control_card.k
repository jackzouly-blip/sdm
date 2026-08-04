*KEYWORD
$> Include file: F:\00_word\CAB_0729\test\11111\03_Control_card.k
$> (Used by master file: F:\00_word\CAB_0729\test\11111\main.key)
$> 
$> Primer17.1 created on: Thu Jul 30 01:47:09 2026
$> LS-DYNA version: 971R3
$> 
$> end_saved_comments
$
$
$
$ =============
$ CONTROL cards
$ =============
$
*CONTROL_ACCURACY
$$ OSU           INN    PIDOSU
         1         2         0
*CONTROL_BULK_VISCOSITY
$$      Q1        Q2      TYPE     BTYPE
       1.5      0.06        -1
*CONTROL_CONTACT
$$  SLSFAC    RWPNAL    ISLCHK    SHLTHK    PENOPT    THKCHG     ORIEN    ENMASS
       0.1       1.0         1         0         1         0         1         0
$$  USRSTR    USRFRC     NSBCS    INTERM     XPENE     SSTHK      ECDT   TIEDPRJ
         0         0        16         0       4.0         0         0         0
$$   SFRIC     DFRIC       EDC    INTVFC        TH     TH_SF    PEN_SF
       0.0       0.0       0.0       0.0       0.0       0.0       0.0
$$  IGNORE    FRCENG   SKIPRWG    OUTSEG   SPOTSTP   SPOTDEL   SPOTHIN
         1         0         0         0         0         0       0.0
*CONTROL_ENERGY
$$    HGEN      RWEN    SLNTEN     RYLEN
         2         2         2         2
*CONTROL_HOURGLASS
$$     IHQ        QH
         4      0.05
*CONTROL_OUTPUT
$$   NPOPT    NEECHO    NREFUP    IACCOP     OPIFS    IPNINT    IKEDIT    IFLUSH
         1         3         0         0       0.0         0       200      5000
$$   IPRTF    IERODE     TET10    MSGMAX    IPCURV      GMDT   IP1DBLT      EOCS
         0         0         1         0         0
*CONTROL_SHELL
$$  WRPANG     ESORT     IRNXX    ISTUPD    THEORY       BWC     MITER      PROJ
      20.0         1        -1         0         2         1         1         0
*CONTROL_SOLID
$$   ESORT    FMATRX   NIPTETS    SWLOCL    PSFAIL   T10JTOL
         1         1         4         1
*CONTROL_TERMINATION
$$  ENDTIM    ENDCYC     DTMIN    ENDENG    ENDMAS     NOSOL
     120.0         0       0.0       0.0       0.0
*CONTROL_TIMESTEP
$$  DTINIT    TSSFAC      ISDO    TSLIMT     DT2MS      LCTM     ERODE     MSIST
       0.0       0.9         0       0.0   -5.5E-4         0         0         0
$
$
$ ==============
$ DATABASE cards
$ ==============
$
*DATABASE_ABSTAT
$$      DT    BINARY      LCUR      IOPT
       0.1         3         0         0
*DATABASE_BNDOUT
$$      DT    BINARY      LCUR      IOPT
       0.1         3         0         0
*DATABASE_DEFORC
$$      DT    BINARY      LCUR      IOPT
       0.1         3         0         0
*DATABASE_ELOUT
$$      DT    BINARY      LCUR      IOPT
       0.1         3         0         0
*DATABASE_GLSTAT
$$      DT    BINARY      LCUR      IOPT
       0.1         3         0         0
*DATABASE_JNTFORC
$$      DT    BINARY      LCUR      IOPT
       0.1         3         0         0
*DATABASE_MATSUM
$$      DT    BINARY      LCUR      IOPT
       0.1         3         0         0
*DATABASE_NODFOR
$$      DT    BINARY      LCUR      IOPT
       0.1         3         0         0
*DATABASE_NODOUT
$$      DT    BINARY      LCUR      IOPT      DTHF     BINHF
       0.1         3         0         0       0.0         0
*DATABASE_RBDOUT
$$      DT    BINARY      LCUR      IOPT
       0.1         3         0         0
*DATABASE_RWFORC
$$      DT    BINARY      LCUR      IOPT
       0.1         3         0         0
*DATABASE_SECFORC
$$      DT    BINARY      LCUR      IOPT
       0.1         3         0         0
*DATABASE_RCFORC
$$      DT    BINARY      LCUR      IOPT
       0.1         3         0         0
*DATABASE_SBTOUT
$$      DT    BINARY      LCUR      IOPT
       0.1         3         0         0
*DATABASE_SLEOUT
$$      DT    BINARY      LCUR      IOPT
       0.1         3         0         0
*DATABASE_SPCFORC
$$      DT    BINARY      LCUR      IOPT
       0.1         3         0         0
*DATABASE_BINARY_D3DUMP
$$ DT/CYCL        NR
    500000
*DATABASE_BINARY_D3PLOT
$$ DT/CYCL      LCDT      BEAM     NPLTC    PSETID
       2.5         0         0         0
*DATABASE_BINARY_D3THDT
$$ DT/CYCL      LCID
       0.1         0
*DATABASE_BINARY_INTFOR
$$ DT/CYCL      LCID
       5.0         0
*DATABASE_EXTENT_BINARY
$$   NEIPH     NEIPS    MAXINT    STRFLG    SIGFLG    EPSFLG    RLTFLG    ENGFLG
         0         0         0         0         1         1         1         1
$$  CMPFLG    IEVERP    BEAMIP     DCOMP      SHGE     STSSZ    N3THDT   IALEMAT
         0         1         0         0         0         0         0         0
$$ NINTSLD   PKP_SEN      SCLP     HYDRO     MSSCL     THERM    INTOUT    NODOUT
         0         0       0.0         0         0         0
$$    DTDT    RESPLT     NEIPB
$
*END
