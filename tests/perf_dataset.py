"""Deterministic synthetic apartments. No external data or Cian requests."""
from pathlib import Path
import random
from PIL import Image


FEATURES=('окна во двор','кухня отдельная','нужен ремонт ванной','проверить перепланировку',
          'соседи сверху','подъезд после ремонта','пешком до станции','лоджия застеклена',
          'первичные документы','собственность с 2014 года','свободная продажа','ипотека возможна',
          'проверить вентиляцию','слышимость в спальне','этаж выше середины','парковка вечером',
          'детская площадка','магазины рядом','раздельный санузел','лифт пассажирский',
          'выход на две стороны','школа через дорогу','стояки заменены','вид из гостиной',
          'точный метраж','остекление балкона','проверить электрику','срок освобождения',
          'согласовать второй визит','проверить выписку','сравнить цену','торг после осмотра')

def note(seed_value,length,prefix):
    rng=random.Random(seed_value);parts=[prefix]
    while len(' '.join(parts))<length:
        parts.append(rng.choice(FEATURES)+' — '+str(rng.randrange(1,500))+'.')
    return ' '.join(parts)[:length].rstrip()

def realtor_note(index):
    return note(index+9000,900,'Риелтор проверил квартиру '+str(index+1)+'.')


def seed(main,count=500):
    stamp='2026-11-01T10:00:00+00:00'
    with main.database() as conn:
        items=[{'id':str(800000+i),'url':'https://www.cian.ru/sale/flat/'+str(800000+i)+'/',
                'title':'1-комнатная квартира','address':'Москва, Тестовая улица, дом '+str(i+1),
                'complex_name':'ЖК Проверочный','price':12000000+i*1000,'area':36.5,'floor':'7/16',
                'metro':('Сокол','Перово','Динамо','Аэропорт','Измайловская')[i%5],'metro_time':str(8+i%13)+' мин. пешком',
                'description':note(i,2000,'Синтетическое длинное описание квартиры '+str(i+1)+'.'),
                'card_text':note(i+6000,1800,'Синтетический DOM-текст карточки '+str(i+1)+'.')}
               for i in range(count)]
        main.save_items(conn,items,stamp)
        user=conn.execute("SELECT id FROM users WHERE role='admin' LIMIT 1").fetchone()[0]
        for i,item in enumerate(items):
            pid=conn.execute('SELECT property_id FROM listings WHERE id=?',(item['id'],)).fetchone()[0]
            selected=i<20
            status='rejected' if i%11==0 else 'liked' if i%3==0 else 'new'
            conn.execute('UPDATE listings SET review_status=?,realtor_comment=?,realtor_version=1,realtor_updated_by=?,realtor_updated_at=? WHERE id=?',
                         (status,realtor_note(i),user,stamp,item['id']))
            conn.execute('UPDATE properties SET in_our_list=?,selection_version=1,family_comment=?,family_comment_version=1,family_comment_updated_by=?,family_comment_updated_at=? WHERE id=?',
                         (selected,note(i+3000,1900,'Общий комментарий семьи '+str(i+1)+'.'),user,stamp,pid))
            for j in range(12):
                conn.execute('INSERT INTO price_history(property_id,listing_id,observed_at,price) VALUES (?,?,?,?)',
                             (pid,item['id'],'2026-10-'+str(j+1).zfill(2)+'T10:00:00+00:00',12000000+j*10000))
            for j in range(6):
                conn.execute("""INSERT INTO property_comment_history(property_id,origin_property_id,origin_listing_id,kind,text,changed_at,changed_by,version)
                                VALUES (?,?,?,?,?,?,?,?)""",
                             (pid,pid,item['id'],'family',note(i*10+j+20000,1900,'Исторический комментарий '+str(j+1)+' к квартире '+str(i+1)+'.'),stamp,user,j))
            for j in range(3):
                conn.execute("""INSERT INTO property_visits(property_id,status,outcome,completed_at,preparation,comment,
                    created_at,updated_at,updated_by,version,rejection_reasons) VALUES (?,'completed',?,?,?,?,?,?,?,1,'[]')""",
                    (pid,'liked' if i%2 else 'rejected','2026-10-'+str(j+10).zfill(2)+'T12:00:00+00:00',
                     note(i*10+j+30000,600,'Заметка до поездки.'),note(i*10+j+40000,1000,'Впечатления после просмотра.'),stamp,stamp,user))
            if selected:
                conn.execute("""INSERT INTO property_visits(property_id,status,scheduled_at,created_at,updated_at,updated_by,version)
                                VALUES (?,'planned','2030-01-01T15:00:00+00:00',?,?,?,1)""",(pid,stamp,stamp,user))
            conn.execute('UPDATE properties SET visits_version=4 WHERE id=?',(pid,))
            version='a'*24
            folder=main.DATA/'images'/'listings';folder.mkdir(parents=True,exist_ok=True)
            for size,dimensions in [('normal',(640,400)),('thumb',(220,160))]:
                relative='images/listings/'+item['id']+'-'+version+'-'+size+'.jpg'
                with Image.new('RGB',dimensions,'#769d70') as image:image.save(main.DATA/relative)
                conn.execute('UPDATE listings SET '+('cover_image_path' if size=='normal' else 'cover_thumb_path')+'=? WHERE id=?',(relative,item['id']))
    return items
