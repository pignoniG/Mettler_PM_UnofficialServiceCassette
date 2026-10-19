"""Minimal 8051 recursive-descent disassembler. Image loaded at BASE."""
import sys, collections
BASE = 0x8000
SFR = {0x80:'P0',0x81:'SP',0x82:'DPL',0x83:'DPH',0x87:'PCON',0x88:'TCON',0x89:'TMOD',0x8A:'TL0',0x8B:'TL1',
 0x8C:'TH0',0x8D:'TH1',0x90:'P1',0x98:'SCON',0x99:'SBUF',0xA0:'P2',0xA8:'IE',0xB0:'P3',0xB8:'IP',0xC8:'T2CON',
 0xCA:'RCAP2L',0xCB:'RCAP2H',0xCC:'TL2',0xCD:'TH2',0xD0:'PSW',0xE0:'ACC',0xF0:'B'}
BITSFR = {0x88:['IT0','IE0','IT1','IE1','TR0','TF0','TR1','TF1'],0x98:['RI','TI','RB8','TB8','REN','SM2','SM1','SM0'],
 0xA8:['EX0','ET0','EX1','ET1','ES','ET2','b6','EA'],0xD0:['P','F1','OV','RS0','RS1','F0','AC','CY'],
 0xC8:['CPRL2','CT2','TR2','EXEN2','TCLK','RCLK','EXF2','TF2']}
def dname(a): return SFR.get(a, '%02Xh'%a) if a>=0x80 else '%02Xh'%a
def bname(b):
    if b<0x80: return '%02Xh.%d'%(0x20+(b>>3), b&7) + ('/b%02X'%b)
    byte=b&0xF8; bit=b&7
    if byte in BITSFR: return BITSFR[byte][bit]
    return '%s.%d'%(dname(byte),bit)
REGS=['R0','R1','R2','R3','R4','R5','R6','R7']
def decode(mem, pc):
    """returns (length, text, targets, flow) flow: 'jmp','ret','call','cond',None"""
    o=mem(pc); b1=mem(pc+1); b2=mem(pc+2)
    rel=lambda x,l: (pc+l+(x-256 if x>127 else x))&0xFFFF
    lo=o&0xF; hi=o>>4
    if lo==1:
        tgt=(((pc+2)&0xF800)|((o>>5)<<8)|b1)
        return (2,('ACALL' if o&0x10 else 'AJMP')+' %04Xh'%tgt,[tgt],'call' if o&0x10 else 'jmp')
    if lo>=8:
        r=REGS[lo-8]
        t={0:'INC %s',1:'DEC %s',2:'ADD A,%s',3:'ADDC A,%s',4:'ORL A,%s',5:'ANL A,%s',6:'XRL A,%s',9:'SUBB A,%s',0xC:'XCH A,%s',0xE:'MOV A,%s',0xF:'MOV %s,A'}
        if hi in t: return (1,t[hi]%r,[],None)
        if hi==7: return (2,'MOV %s,#%02Xh'%(r,b1),[],None)
        if hi==8: return (2,'MOV %s,%s'%(dname(b1),r),[],None)
        if hi==0xA: return (2,'MOV %s,%s'%(r,dname(b1)),[],None)
        if hi==0xB: return (3,'CJNE %s,#%02Xh,%04Xh'%(r,b1,rel(b2,3)),[rel(b2,3)],'cond')
        if hi==0xD: return (2,'DJNZ %s,%04Xh'%(r,rel(b1,2)),[rel(b1,2)],'cond')
    if lo in (6,7):
        r='@R%d'%(lo-6)
        t={0:'INC %s',1:'DEC %s',2:'ADD A,%s',3:'ADDC A,%s',4:'ORL A,%s',5:'ANL A,%s',6:'XRL A,%s',9:'SUBB A,%s',0xC:'XCH A,%s',0xD:'XCHD A,%s',0xE:'MOV A,%s',0xF:'MOV %s,A'}
        if hi in t: return (1,t[hi]%r,[],None)
        if hi==7: return (2,'MOV %s,#%02Xh'%(r,b1),[],None)
        if hi==8: return (2,'MOV %s,%s'%(dname(b1),r),[],None)
        if hi==0xA: return (2,'MOV %s,%s'%(r,dname(b1)),[],None)
        if hi==0xB: return (3,'CJNE %s,#%02Xh,%04Xh'%(r,b1,rel(b2,3)),[rel(b2,3)],'cond')
    if lo==5:
        d=dname(b1)
        t={0:'INC %s',1:'DEC %s',2:'ADD A,%s',3:'ADDC A,%s',4:'ORL A,%s',5:'ANL A,%s',6:'XRL A,%s',9:'SUBB A,%s',0xC:'XCH A,%s',0xE:'MOV A,%s',0xF:'MOV %s,A',0xC:'XCH A,%s'}
        if hi in t: return (2,t[hi]%d,[],None)
        if hi==7: return (3,'MOV %s,#%02Xh'%(d,b2),[],None)
        if hi==8: return (3,'MOV %s,%s'%(dname(b2),d),[],None)
        if hi==0xA: return (1,'???',[],'ret')
        if hi==0xB: return (3,'CJNE A,%s,%04Xh'%(d,rel(b2,3)),[rel(b2,3)],'cond')
        if hi==0xC: return (2,'XCH A,%s'%d,[],None)
        if hi==0xD: return (3,'DJNZ %s,%04Xh'%(d,rel(b2,3)),[rel(b2,3)],'cond')
    if lo==4:
        t={0:'INC A',1:'DEC A',2:(2,'ADD A,#%02Xh'),3:(2,'ADDC A,#%02Xh'),4:(2,'ORL A,#%02Xh'),5:(2,'ANL A,#%02Xh'),6:(2,'XRL A,#%02Xh'),
           7:(2,'MOV A,#%02Xh'),8:'DIV AB',9:(2,'SUBB A,#%02Xh'),0xA:'MUL AB',0xC:'SWAP A',0xD:'DA A',0xE:'CLR A',0xF:'CPL A'}
        if hi==0xB: return (3,'CJNE A,#%02Xh,%04Xh'%(b1,rel(b2,3)),[rel(b2,3)],'cond')
        v=t[hi]
        if isinstance(v,tuple): return (2,v[1]%b1,[],None)
        return (1,v,[],None)
    if lo==3:
        d=dname(b1)
        t={0:(1,'RR A'),1:(1,'RRC A'),2:(1,'RL A'),3:(1,'RLC A'),4:(3,'ORL %s,#%02Xh'%(d,b2)),5:(3,'ANL %s,#%02Xh'%(d,b2)),6:(3,'XRL %s,#%02Xh'%(d,b2)),
           7:(2,'JMP @A+DPTR'),8:(1,'MOVC A,@A+PC'),9:(1,'MOVC A,@A+DPTR'),0xA:(2,'CPL C'),0xB:(2,'CPL C'),0xC:(1,'SETB C'),0xD:(2,'SETB C'),0xE:(1,'MOVX A,@R1'),0xF:(1,'MOVX @R1,A')}
        if hi==7: return (1,'JMP @A+DPTR',[],'ret')
        if hi==0xA: return (1,'INC DPTR',[],None)
        if hi==0xB: return (1,'CPL C',[],None)
        if hi==0xC: return (1,'CLR C',[],None)
        if hi==0xD: return (1,'SETB C',[],None)
        if hi==0x8: return (1,'MOVC A,@A+PC',[],None)
        if hi==0x9: return (1,'MOVC A,@A+DPTR',[],None)
        return (t[hi][0],t[hi][1],[],None)
    if lo==2:
        d=dname(b1)
        if hi==0: return (3,'LJMP %04Xh'%(b1<<8|b2),[b1<<8|b2],'jmp')
        if hi==1: return (3,'LCALL %04Xh'%(b1<<8|b2),[b1<<8|b2],'call')
        if hi==2: return (1,'RET',[],'ret')
        if hi==3: return (1,'RETI',[],'ret')
        t={4:'ORL %s,A',5:'ANL %s,A',6:'XRL %s,A'}
        if hi in t: return (2,t[hi]%d,[],None)
        if hi==7: return (2,'ORL C,%s'%bname(b1),[],None)
        if hi==8: return (2,'ANL C,%s'%bname(b1),[],None)
        if hi==9: return (2,'MOV %s,C'%bname(b1),[],None)
        if hi==0xA: return (2,'MOV C,%s'%bname(b1),[],None)
        if hi==0xB: return (2,'CPL %s'%bname(b1),[],None)
        if hi==0xC: return (2,'CLR %s'%bname(b1),[],None)
        if hi==0xD: return (2,'SETB %s'%bname(b1),[],None)
        if hi==0xE: return (1,'MOVX A,@R0',[],None)
        if hi==0xF: return (1,'MOVX @R0,A',[],None)
    if lo==0:
        if hi==0: return (1,'NOP',[],None)
        if hi in (1,2,3):
            n={1:'JBC',2:'JB',3:'JNB'}[hi]; return (3,'%s %s,%04Xh'%(n,bname(b1),rel(b2,3)),[rel(b2,3)],'cond')
        if hi==4: return (2,'JC %04Xh'%rel(b1,2),[rel(b1,2)],'cond')
        if hi==5: return (2,'JNC %04Xh'%rel(b1,2),[rel(b1,2)],'cond')
        if hi==6: return (2,'JZ %04Xh'%rel(b1,2),[rel(b1,2)],'cond')
        if hi==7: return (2,'JNZ %04Xh'%rel(b1,2),[rel(b1,2)],'cond')
        if hi==8: return (2,'SJMP %04Xh'%rel(b1,2),[rel(b1,2)],'jmp')
        if hi==9: return (3,'MOV DPTR,#%04Xh'%(b1<<8|b2),[],None)
        if hi==0xA: return (2,'ORL C,/%s'%bname(b1),[],None)
        if hi==0xB: return (2,'ANL C,/%s'%bname(b1),[],None)
        if hi==0xC: return (2,'PUSH %s'%dname(b1),[],None)
        if hi==0xD: return (2,'POP %s'%dname(b1),[],None)
        if hi==0xE: return (1,'MOVX A,@DPTR',[],None)
        if hi==0xF: return (1,'MOVX @DPTR,A',[],None)
    raise Exception(hex(o))

def disasm(img, entries):
    mem=lambda a: img[a-BASE] if BASE<=a<BASE+len(img) else 0xFF
    code={}; todo=list(entries); xref=collections.defaultdict(set); calls=set(entries)
    while todo:
        pc=todo.pop()
        while BASE<=pc<BASE+len(img) and pc not in code:
            l,t,tg,fl=decode(mem,pc)
            code[pc]=(l,t)
            for x in tg:
                xref[x].add(pc)
                if fl=='call': calls.add(x)
                todo.append(x)
            if fl in ('jmp','ret'): break
            pc+=l
    return code, xref, calls
if __name__=='__main__':
    img=open(sys.argv[1],'rb').read()
    ents=[int(x,16) for x in sys.argv[2:]] or [0x8000]
    code,xref,calls=disasm(img,ents)
    out=[]
    for pc in sorted(code):
        l,t=code[pc]
        if pc in calls: out.append('\n;---------------- sub_%04X'%pc)
        lab = 'L%04X:'%pc if pc in xref else ''
        byts=' '.join('%02X'%img[pc-BASE+i] for i in range(l))
        refs=(' ; xref '+','.join('%04X'%x for x in sorted(xref[pc])[:6])) if pc in xref else ''
        out.append('%-7s %04X  %-9s %s%s'%(lab,pc,byts,t,refs))
    print('\n'.join(out))
    print('; coverage %d bytes'%sum(v[0] for v in code.values()), file=sys.stderr)
