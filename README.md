# buyspotshortfutureaster
gain point for airdrop stage3
ระบบจะรักษาเงินทุนไว้ทั้งหมด จะเพิ่มยอดคะแนน ใช้เงินต้นต่ำ น่าจะคุ้มกับ Airdrop



aster-maker15.py  (Clean & Full)
- เปิดคู่แบบ Maker: BUY Spot + SELL Futures (short) ด้วย GTX (ไม่กินคิว)
- แนบโพสิชันที่มี (attach-existing): เติม/ตัดให้เข้าเป้าทุน (target qty) พร้อมกันสองฝั่ง
- วัด 'กำไรสุทธิหลังปิด' = open_basis - open_fee + close_gain - close_fee - slippage
- ถึงเป้า → ปิดคู่ (maker-first, มี IOC/MARKET fallback) แล้วเปิดใหม่อัตโนมัติ (ถ้า --always-reopen)
- บังคับกริดทุกคำสั่ง (qty ตาม LOT_SIZE.step, price ตาม tick) เพื่อกัน "-1111 Precision"
- ตรวจ notional ขั้นต่ำ 5 USDT ทั้ง Spot/Futures ก่อนยิง
- ป้องกันปิดในตลาดบาง (สเปรด/เด็ปธ์) + ยืนยันกำไรต่อเนื่อง (confirm-hits)
- มี retry/backoff/cooldown + reset state อัตโนมัติเมื่อ error
- Log สี: ฟ้า(ข้อมูล), เขียว(ซื้อ/กำไร), แดง(ขาย/ขาดทุน/ข้อผิดพลาด), ส้ม(เตือน)


1.สร้างบัญชี ที่ asterdex ก่อน 

https://www.asterdex.com/en/referral/F40b58

2. โอนเงินเข้าผ่าน Bsc ก็ได้ ถูกใช้ USDT 

3. โอนเข้า SPOT ขั้นต่ำ 100$ และโอนไป Future 70$ ก็พอ ก็ทำคะแนนได้แล้ว 

4. ถ้าราคา aster ขึ้น เงินจะโตฝั่ง Spot  , ถ้า ราคา aster ลงเงินจะโตฝั่ง future ก็โอนเงินให้ balance ครับ 

ENV ที่ต้องมี: ASTERDEX_API_KEY, ASTERDEX_API_SECRET  

ใส่ใน .env 
ASTERDEX_API_KEY=change to your key
ASTERDEX_API_SECRET=change to your secret


Easy run with python3
1. create .env โดย ทำตามนี้ ก็ได้
   
   mkdir aster
   
   cd aster

   vi .env 

   set -a

   source .env

   set +a

3. backgound running  **** แก้ทุนตามที่ตัวเองจะ run นะครับ เริ่มน้อยๆ พอไม่มีปัญหา ก็แล้วแต่เลยครับ ในนี้ใส่ให้เป็น 120$ และ ปิดทุกๆ กำไร 1$ หรือ ใกล้เคียง 

nohup python3 aster-maker15.py --symbol ASTERUSDT --asset ASTER --capital 120 --target-profit 1 --maker-spot-bps 0 --maker-fut-bps 0 --taker-spot-bps 5 --taker-fut-bps 5 --slippage-bps 3 --depth-limit 10 --isolated --leverage 4 --poll 3 --cooldown-sec 2 --log-level INFO > maker15.log 2>&1 &

or shell running

python3 aster-maker15.py --symbol ASTERUSDT --asset ASTER --capital 120 --target-profit 1 --maker-spot-bps 0 --maker-fut-bps 0 --taker-spot-bps 5 --taker-fut-bps 5 --slippage-bps 3 --depth-limit 10 --isolated --leverage 4 --poll 3 --cooldown-sec 2 --log-level INFO > maker15.log 2>&1

ติดอะไรก็ทักมา 7-11
