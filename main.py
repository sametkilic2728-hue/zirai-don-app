import flet as ft
import requests
import datetime
from geopy.geocoders import Nominatim
import pymysql
from threading import Thread
import time

try:
    from flet_geolocator import Geolocator, GeolocatorSettings
    GEOLOCATOR_AVAILABLE = True
    print("flet-geolocator yuklü")
except ImportError:
    GEOLOCATOR_AVAILABLE = False
    print("GPS devre disi - IP kullanilacak")

kritik_sicaklik = -1.5
nem_maks_radyasyon = 80
ruzgar_maks_radyasyon = 5.0

vt_ayarlari = {
    "host": "89.252.183.179",
    "user": "webcina1_ziraidonproje",
    "password": "Zp~@0PFF5j8u=S3N",
    "database": "webcina1_ziraidon",
    "port": 3306
}


class GlobalState:
    def __init__(self):
        self.guncel_uyarilar = []
        self.guncel_sehir = ""
        self.guncel_ilce = ""
        self.guncel_tum_veriler = []
        self.arduino_verileri = []
        self.guncel_enlem = 0.0
        self.guncel_boylam = 0.0


global_state = GlobalState()


def main(page: ft.Page):
    page.title = "Zirai Don Uyari Sistemi"
    page.theme_mode = ft.ThemeMode.DARK
    page.padding = 0
    page.bgcolor = "#07121a"

    geolocator_widget = None
    if GEOLOCATOR_AVAILABLE:
        try:
            settings = GeolocatorSettings(
                accuracy="best_for_navigation",
                distance_filter=0,
            )
            geolocator_widget = Geolocator(settings=settings)
            print("GPS: YUKSEK HASSASIYET MODU")
        except Exception:
            geolocator_widget = Geolocator()
            print("GPS: Standart mod")

    turkiye_illeri = ["Gaziantep"]
    il_ilce_dict = {
        "Gaziantep": ["Sehitkamil", "Sahinbey", "Islahiye", "Nizip", "Araban",
                      "Karkamis", "Nurdag", "Oguzeli", "Yavuzeli"],
    }

    renk_acik_mavi = "#4a90c7"
    renk_orta_mavi = "#2d5a7b"
    renk_koyu_kart = "#0f2433"
    renk_kart_variant = "#132f45"
    renk_yazi_acik = "#d4e4f7"
    renk_hint = "#9fb5c8"

    def hava_durumu_al(lat, lon, gun=3):
        url = "https://api.open-meteo.com/v1/forecast"
        parametreler = {
            "latitude": lat,
            "longitude": lon,
            "hourly": "temperature_2m,relativehumidity_2m,windspeed_10m,cloudcover",
            "forecast_days": gun,
            "timezone": "Europe/Istanbul",
            "windspeed_unit": "ms"
        }
        yanit = requests.get(url, params=parametreler, timeout=20)
        yanit.raise_for_status()
        return yanit.json()

    def donu_analiz(tahmin_json):
        saatlik = tahmin_json["hourly"]
        saatler = saatlik["time"]
        sicakliklar = saatlik["temperature_2m"]
        nemler = saatlik["relativehumidity_2m"]
        ruzgarlar = saatlik["windspeed_10m"]
        uyarilar = []
        tum_veriler = []

        for i, t_iso in enumerate(saatler):
            sicaklik_dht = sicakliklar[i]
            nem = nemler[i]
            ruzgar = ruzgarlar[i]
            # sicaklik_lm35: Arduino LM35 sensoru yoksa DHT ile ayni deger kullanilir
            sicaklik_lm35 = sicaklik_dht
            tum_veriler.append({
                "zaman": t_iso,
                "sicaklik_dht": sicaklik_dht,
                "sicaklik_lm35": sicaklik_lm35,
                "nem": nem,
                "ruzgar": ruzgar,
                "durum": "normal"
            })
            if sicaklik_dht <= kritik_sicaklik:
                dt = datetime.datetime.fromisoformat(t_iso)
                saat = dt.hour
                gece_saati_mi = saat >= 20 or saat <= 6
                if (gece_saati_mi and
                        ruzgar <= ruzgar_maks_radyasyon and
                        nem <= nem_maks_radyasyon):
                    risk_tipi = "Radyasyon Donu"
                    oneri = "Gece ortuleme, fan veya sisleme/sulama yapilmali."
                else:
                    risk_tipi = "Advektif Don"
                    oneri = "Toprak nemi artirilmali, ruzgar kiranlar kullanilabilir."
                uyarilar.append({
                    "zaman": t_iso,
                    "sicaklik_dht": sicaklik_dht,
                    "sicaklik_lm35": sicaklik_lm35,
                    "nem": nem,
                    "ruzgar": ruzgar,
                    "risk": risk_tipi,
                    "oneri": oneri,
                })
                tum_veriler[-1]["durum"] = risk_tipi
        return uyarilar, tum_veriler

    def vt_kaydet(uyarilar, tum_veriler, enlem, boylam):
        try:
            baglanti = pymysql.connect(**vt_ayarlari, connect_timeout=5)
            imle = baglanti.cursor()
            analiz_zamani = datetime.datetime.now().isoformat()
            if uyarilar:
                for u in uyarilar:
                    imle.execute(
                        """INSERT INTO veriler
                        (sicaklik_dht, nem, sicaklik_lm35, enlem, boylam, tarih)
                        VALUES (%s, %s, %s, %s, %s, %s)""",
                        (u["sicaklik_dht"], u["nem"], u["sicaklik_lm35"],
                         enlem, boylam, analiz_zamani)
                    )
            else:
                if tum_veriler:
                    min_veri = min(tum_veriler, key=lambda x: x["sicaklik_dht"])
                    imle.execute(
                        """INSERT INTO veriler
                        (sicaklik_dht, nem, sicaklik_lm35, enlem, boylam, tarih)
                        VALUES (%s, %s, %s, %s, %s, %s)""",
                        (min_veri["sicaklik_dht"], min_veri["nem"],
                         min_veri["sicaklik_lm35"], enlem, boylam, analiz_zamani)
                    )
            baglanti.commit()
            imle.close()
            baglanti.close()
            print(f"VT kaydedildi: enlem={enlem}, boylam={boylam}")
            return True
        except Exception as e:
            print(f"VT hatasi: {e}")
            return False

    def arduino_verilerini_al(tarih=None):
        try:
            veri_list = [
                {
                    "zaman": str(datetime.datetime.now()),
                    "sicaklik_dht": 9.3,
                    "sicaklik_lm35": 9.1,
                    "toprak_nemi": 73,
                    "sehir": "Gaziantep",
                    "ilce": "Sehitkamil"
                }
            ]
            return veri_list
        except Exception as e:
            print(f"Arduino verisi hatasi: {e}")
            return []

    def snackbar_goster(mesaj, bgcolor="blue"):
        sb = ft.SnackBar(
            content=ft.Text(mesaj, color="white"),
            bgcolor=bgcolor
        )
        page.overlay.append(sb)
        sb.open = True
        page.update()

    def create_appbar():
        menu_state = {"acik": False}
        menu_column = ft.Column(visible=False, spacing=0)

        def menu_toggle(e):
            menu_state["acik"] = not menu_state["acik"]
            menu_column.visible = menu_state["acik"]
            page.update()

        menu_column.controls = [
            ft.Container(height=8),
            ft.TextButton(
                "Ana Sayfa",
                on_click=lambda _: (menu_toggle(None), page._custom_go("/")),
                style=ft.ButtonStyle(color=renk_yazi_acik)
            ),
            ft.TextButton(
                "Gecmis Kayitlar",
                on_click=lambda _: (menu_toggle(None), page._custom_go("/gecmis")),
                style=ft.ButtonStyle(color=renk_yazi_acik)
            ),
            ft.Divider(height=1, color="#2d5a7b"),
            ft.TextButton(
                "Hakkimizda",
                on_click=lambda _: (menu_toggle(None), page._custom_go("/hakkimizda")),
                style=ft.ButtonStyle(color=renk_hint)
            ),
            ft.Container(height=8),
        ]

        return ft.Column(
            controls=[
                ft.Container(
                    content=ft.Row(
                        controls=[
                            ft.IconButton(
                                icon=ft.icons.Icons.MENU,
                                icon_color="white",
                                on_click=menu_toggle
                            ),
                            ft.Text(
                                "Zirai Don",
                                size=18,
                                weight=ft.FontWeight.BOLD,
                                color="white"
                            ),
                            ft.IconButton(
                                icon=ft.icons.Icons.REFRESH,
                                on_click=lambda e: page._custom_go(page.route),
                                icon_color="white"
                            )
                        ],
                        alignment=ft.MainAxisAlignment.SPACE_BETWEEN
                    ),
                    padding=ft.padding.symmetric(horizontal=16, vertical=10),
                    bgcolor="#071a26",
                    width=float('inf'),
                ),
                ft.Container(
                    content=menu_column,
                    bgcolor="#132f45",
                    padding=ft.padding.symmetric(horizontal=10),
                    width=float('inf'),
                    border_radius=ft.border_radius.vertical(bottom=8),
                )
            ],
            spacing=0
        )

    def ana_sayfa_olustur():
        ilerleme_halkasi = ft.ProgressRing(visible=False, color=renk_acik_mavi)
        durum_metni = ft.Text(
            "",
            size=12,
            color=renk_hint,
            text_align=ft.TextAlign.CENTER
        )

        def gps_izni_iste():
            if GEOLOCATOR_AVAILABLE and geolocator_widget:
                try:
                    permission = geolocator_widget.request_permission()
                    permission_str = str(permission)
                    if ("DENIED" not in permission_str and
                            "RESTRICTED" not in permission_str):
                        snackbar_goster("GPS izni verildi", "green")
                    else:
                        snackbar_goster(
                            "GPS izni gereklidir. Ayarlardan aktiflestiriniz.",
                            "orange"
                        )
                except Exception as e:
                    print(f"GPS izni hatasi: {e}")

        def konum_bul_otomatik():
            ilerleme_halkasi.visible = True
            konum_bul_dugmesi.disabled = True
            durum_metni.value = "Konum aliniyor..."
            page.update()

            def konum_isle():
                lat = None
                lon = None
                konum_tipi = "IP"

                try:
                    if GEOLOCATOR_AVAILABLE and geolocator_widget:
                        try:
                            permission = geolocator_widget.request_permission()
                            permission_str = str(permission)
                            if ("DENIED" not in permission_str and
                                    "RESTRICTED" not in permission_str):
                                durum_metni.value = "GPS sinyali aranıyor..."
                                page.update()
                                time.sleep(2)
                                position = geolocator_widget.get_current_position(
                                    accuracy=5
                                )
                                if position and hasattr(position, 'latitude'):
                                    lat = position.latitude
                                    lon = position.longitude
                                    accuracy = getattr(position, 'accuracy', 0)
                                    konum_tipi = "GPS"
                                    snackbar_goster(
                                        f"GPS: {accuracy:.0f}m hassasiyet",
                                        "green"
                                    )
                        except Exception as e:
                            print(f"GPS hatasi: {e}")

                    if not lat or not lon:
                        durum_metni.value = "IP konumu aliniyor..."
                        page.update()
                        try:
                            response = requests.get(
                                "https://ipapi.co/json/", timeout=10
                            )
                            data = response.json()
                            lat = data.get('latitude')
                            lon = data.get('longitude')
                            if not lat or not lon:
                                response = requests.get(
                                    "https://ipinfo.io/json", timeout=10
                                )
                                data = response.json()
                                loc = data.get("loc", "").split(",")
                                lat = float(loc[0])
                                lon = float(loc[1])
                        except Exception as e:
                            raise ValueError(f"Konum bulunamadi: {e}")

                    sehir = "Gaziantep"
                    ilce = "Sehitkamil"

                    durum_metni.value = f"Sehir bulunuyor ({konum_tipi})..."
                    page.update()

                    try:
                        geocoder = Nominatim(
                            user_agent="zirai_don_app", timeout=30
                        )
                        for zoom_level in [18, 14]:
                            try:
                                location_data = geocoder.reverse(
                                    f"{lat}, {lon}",
                                    language="tr",
                                    zoom=zoom_level,
                                    exactly_one=True
                                )
                                if location_data and hasattr(location_data, 'raw'):
                                    addr = location_data.raw.get('address', {})
                                    ulke = addr.get('country', '')
                                    if 'Turkey' in ulke or 'Turkiye' in ulke:
                                        il_adi = (
                                            addr.get('province') or
                                            addr.get('state') or
                                            addr.get('city') or
                                            addr.get('county') or ''
                                        )
                                        for il in turkiye_illeri:
                                            if il.lower() in il_adi.lower():
                                                sehir = il
                                                break
                                        if sehir in il_ilce_dict:
                                            ilce_adi = (
                                                addr.get('town') or
                                                addr.get('suburb') or
                                                addr.get('neighbourhood') or
                                                addr.get('quarter') or
                                                addr.get('municipality') or
                                                addr.get('city_district') or ''
                                            )
                                            for ilce_k in il_ilce_dict[sehir]:
                                                if ilce_k.lower() in ilce_adi.lower():
                                                    ilce = ilce_k
                                                    break
                                        if sehir != "Gaziantep":
                                            break
                            except Exception as e:
                                print(f"Zoom {zoom_level} hatasi: {e}")
                                continue
                    except Exception as geo_err:
                        print(f"Geocoding hatasi: {geo_err}")
                        # İnternet sorunu varsa varsayılan değerler kullan
                        sehir = "Gaziantep"
                        ilce = "Sehitkamil"

                    durum_metni.value = "Hava verisi analiz ediliyor..."
                    page.update()

                    veriler = hava_durumu_al(lat, lon)
                    uyarilar, tum_veriler = donu_analiz(veriler)
                    vt_kaydet(uyarilar, tum_veriler, lat, lon)

                    global_state.guncel_uyarilar = uyarilar
                    global_state.guncel_sehir = sehir
                    global_state.guncel_ilce = ilce
                    global_state.guncel_tum_veriler = tum_veriler
                    global_state.guncel_enlem = lat
                    global_state.guncel_boylam = lon
                    global_state.arduino_verileri = arduino_verilerini_al()

                    durum_metni.value = "Analiz tamamlandi!"
                    snackbar_goster(f"{sehir} - {ilce} analiz tamamlandi", "green")
                    time.sleep(0.5)
                    page._custom_go("/sonuc")

                except Exception as ex:
                    durum_metni.value = f"Hata: {str(ex)}"
                    snackbar_goster(f"Hata: {str(ex)}", "red")
                    print(f"HATA: {ex}")
                    import traceback
                    traceback.print_exc()
                finally:
                    ilerleme_halkasi.visible = False
                    konum_bul_dugmesi.disabled = False
                    page.update()

            Thread(target=konum_isle, daemon=True).start()

        konum_bul_dugmesi = ft.ElevatedButton(
            "Konumumu Bul ve Analiz Et",
            icon=ft.icons.Icons.LOCATION_ON,
            on_click=lambda _: konum_bul_otomatik(),
            width=320,
            height=60,
            style=ft.ButtonStyle(
                color="white",
                bgcolor="#2ecc71"
            ),
        )

        gecmis_dugmesi = ft.ElevatedButton(
            "Gecmis Kayitlar",
            icon=ft.icons.Icons.HISTORY,
            on_click=lambda _: page._custom_go("/gecmis"),
            width=320,
            height=50,
            style=ft.ButtonStyle(
                color="white",
                bgcolor=renk_orta_mavi
            ),
        )

        ana_icerik = ft.Column(
            controls=[
                create_appbar(),
                ft.Container(height=12),
                ft.Text(
                    "Zirai Don Uyari Sistemi",
                    size=28,
                    weight=ft.FontWeight.BOLD,
                    color="white",
                    text_align=ft.TextAlign.CENTER
                ),
                ft.Container(height=5),
                ft.Text(
                    "Tarimsal Erken Uyari Sistemi",
                    size=13,
                    color=renk_hint,
                    text_align=ft.TextAlign.CENTER
                ),
                ft.Container(height=25),
                konum_bul_dugmesi,
                ft.Container(height=10),
                durum_metni,
                ft.Container(height=5),
                ft.Row(
                    controls=[ilerleme_halkasi],
                    alignment=ft.MainAxisAlignment.CENTER
                ),
                ft.Container(height=20),
                ft.Container(
                    content=ft.Column([
                        ft.Text(
                            "Nasil Calisir?",
                            size=14,
                            weight=ft.FontWeight.BOLD,
                            color="white"
                        ),
                        ft.Container(height=8),
                        ft.Text(
                            "1. Konum Bul butonuna tiklayin\n"
                            "2. Sistem IP uzerinden konumunuzu bulur\n"
                            "3. 3 gunluk hava tahmini yapilir\n"
                            "4. Don riski varsa uyari alirsiniz",
                            size=11,
                            color=renk_yazi_acik
                        ),
                    ], spacing=0),
                    padding=16,
                    bgcolor=renk_kart_variant,
                    border_radius=10,
                    width=320,
                    margin=ft.margin.symmetric(horizontal=20),
                ),
                ft.Container(height=12),
                ft.Container(
                    content=ft.Column([
                        ft.Text(
                            "Gecmis Analiz Sonuclari",
                            size=14,
                            weight=ft.FontWeight.BOLD,
                            color="white"
                        ),
                        ft.Container(height=8),
                        ft.Text(
                            "Daha onceki analizlerinizi goruntuluyin",
                            size=11,
                            color=renk_yazi_acik
                        ),
                        ft.Container(height=10),
                        gecmis_dugmesi,
                    ], spacing=0),
                    padding=16,
                    bgcolor=renk_kart_variant,
                    border_radius=10,
                    width=320,
                    margin=ft.margin.symmetric(horizontal=20),
                ),
                ft.Container(height=15),
            ],
            spacing=0,
            expand=True,
            horizontal_alignment=ft.CrossAxisAlignment.CENTER,
            scroll=ft.ScrollMode.AUTO,
        )

        if geolocator_widget:
            page.overlay.append(geolocator_widget)
            page.update()

            def izin_iste_async():
                time.sleep(1)
                gps_izni_iste()

            Thread(target=izin_iste_async, daemon=True).start()

        return ft.Container(
            content=ana_icerik,
            gradient=ft.LinearGradient(
                begin=ft.Alignment.TOP_LEFT,
                end=ft.Alignment.BOTTOM_RIGHT,
                colors=["#07121a", "#071a26", "#0b2740"],
            ),
            expand=True,
        )

    def sonuc_sayfasi_olustur():
        if global_state.guncel_tum_veriler:
            ortalama = (
                sum(d["sicaklik_dht"] for d in global_state.guncel_tum_veriler) /
                len(global_state.guncel_tum_veriler)
            )
            min_sic = min(d["sicaklik_dht"] for d in global_state.guncel_tum_veriler)
            max_sic = max(d["sicaklik_dht"] for d in global_state.guncel_tum_veriler)
        else:
            ortalama = min_sic = max_sic = 0

        ozet_karti = ft.Container(
            content=ft.Column([
                ft.Icon(
                    ft.icons.Icons.CHECK_CIRCLE
                    if not global_state.guncel_uyarilar
                    else ft.icons.Icons.WARNING,
                    color="green"
                    if not global_state.guncel_uyarilar
                    else "#ff9800",
                    size=60
                ),
                ft.Text(
                    "Don Riski Yok"
                    if not global_state.guncel_uyarilar
                    else "DON UYARISI",
                    size=24,
                    weight=ft.FontWeight.BOLD,
                    color="white"
                ),
                ft.Text(
                    f"{global_state.guncel_sehir} - {global_state.guncel_ilce}",
                    size=14,
                    text_align=ft.TextAlign.CENTER,
                    color=renk_yazi_acik
                ),
                ft.Text(
                    f"Enlem: {global_state.guncel_enlem:.4f}  |  "
                    f"Boylam: {global_state.guncel_boylam:.4f}",
                    size=11,
                    text_align=ft.TextAlign.CENTER,
                    color=renk_hint
                ),
                ft.Container(height=10),
                ft.Text(
                    f"Ortalama DHT: {ortalama:.1f}C",
                    size=13,
                    color=renk_yazi_acik
                ),
                ft.Text(
                    f"Min: {min_sic:.1f}C  |  Max: {max_sic:.1f}C",
                    size=13,
                    color=renk_yazi_acik
                ),
            ],
                horizontal_alignment=ft.CrossAxisAlignment.CENTER,
                spacing=8
            ),
            padding=25,
            bgcolor=renk_koyu_kart,
            border_radius=15,
            width=340,
            margin=ft.margin.symmetric(horizontal=20),
        )

        detay_kartlari = []
        for veri in global_state.guncel_tum_veriler[:24]:
            dt = datetime.datetime.fromisoformat(veri["zaman"])
            tarih_str = dt.strftime("%d.%m %H:%M")
            don_var = veri["durum"] != "normal"
            kart = ft.Container(
                content=ft.Row([
                    ft.Icon(
                        ft.icons.Icons.WARNING if don_var else ft.icons.Icons.CHECK_CIRCLE,
                        color="#ff9800" if don_var else "#4CAF50",
                        size=20
                    ),
                    ft.Column([
                        ft.Text(
                            tarih_str,
                            weight=ft.FontWeight.BOLD,
                            size=12,
                            color="white"
                        ),
                        ft.Text(
                            f"DHT: {veri['sicaklik_dht']:.1f}C  |  "
                            f"LM35: {veri['sicaklik_lm35']:.1f}C  |  "
                            f"Nem: {veri['nem']:.0f}%",
                            size=11,
                            color=renk_yazi_acik
                        ),
                        ft.Text(
                            f"Ruzgar: {veri['ruzgar']:.1f} m/s  |  {veri['durum']}",
                            size=10,
                            color="#ff9800" if don_var else "#4CAF50"
                        ),
                    ], spacing=2, expand=True),
                ], spacing=10),
                padding=10,
                bgcolor="#0b2436",
                border_radius=8,
                margin=ft.margin.symmetric(horizontal=20, vertical=2),
            )
            detay_kartlari.append(kart)

        arduino_karti = ft.Container(content=ft.Column([]), bgcolor="transparent")
        if global_state.arduino_verileri:
            veri = global_state.arduino_verileri[0]
            arduino_karti = ft.Container(
                content=ft.Column([
                    ft.Text(
                        "Arduino Sensor Verileri",
                        size=13,
                        weight=ft.FontWeight.BOLD,
                        color="white"
                    ),
                    ft.Container(height=8),
                    ft.Row([
                        ft.Text(
                            "DHT Sicakligi:",
                            size=12,
                            color=renk_yazi_acik
                        ),
                        ft.Text(
                            f"{veri.get('sicaklik_dht', 0):.1f}C",
                            size=12,
                            weight=ft.FontWeight.BOLD,
                            color="#4CAF50"
                        ),
                    ], spacing=10),
                    ft.Row([
                        ft.Text(
                            "LM35 Sicakligi:",
                            size=12,
                            color=renk_yazi_acik
                        ),
                        ft.Text(
                            f"{veri.get('sicaklik_lm35', 0):.1f}C",
                            size=12,
                            weight=ft.FontWeight.BOLD,
                            color="#FF9800"
                        ),
                    ], spacing=10),
                    ft.Row([
                        ft.Text(
                            "Toprak Nemi:",
                            size=12,
                            color=renk_yazi_acik
                        ),
                        ft.Text(
                            f"{veri.get('toprak_nemi', 0)}%",
                            size=12,
                            weight=ft.FontWeight.BOLD,
                            color="#2196F3"
                        ),
                    ], spacing=10),
                ], spacing=5),
                padding=15,
                bgcolor="#0b3d4d",
                border_radius=10,
                width=340,
                margin=ft.margin.symmetric(horizontal=20, vertical=5),
            )

        return ft.Container(
            content=ft.Column(
                controls=[
                    create_appbar(),
                    ft.Container(
                        content=ft.Row([
                            ft.IconButton(
                                icon=ft.icons.Icons.ARROW_BACK,
                                on_click=lambda _: page._custom_go("/"),
                                icon_color="white"
                            ),
                            ft.Text(
                                f"{global_state.guncel_sehir} - "
                                f"{global_state.guncel_ilce}",
                                size=18,
                                weight=ft.FontWeight.BOLD,
                                color="white"
                            ),
                        ], spacing=10),
                        padding=ft.padding.symmetric(horizontal=10, vertical=8),
                        bgcolor="#071a26",
                        width=float('inf'),
                    ),
                    ft.Column(
                        controls=[
                            ft.Container(height=15),
                            ozet_karti,
                            ft.Container(height=10),
                            arduino_karti,
                            ft.Container(height=10),
                            ft.Text(
                                "72 Saatlik Detay",
                                size=14,
                                weight=ft.FontWeight.BOLD,
                                color="white",
                                text_align=ft.TextAlign.CENTER
                            ),
                            ft.Container(height=5),
                            *detay_kartlari,
                            ft.Container(height=20),
                        ],
                        expand=True,
                        scroll=ft.ScrollMode.AUTO,
                        horizontal_alignment=ft.CrossAxisAlignment.CENTER,
                    ),
                ],
                spacing=0,
                expand=True,
            ),
            gradient=ft.LinearGradient(
                begin=ft.Alignment.TOP_LEFT,
                end=ft.Alignment.BOTTOM_RIGHT,
                colors=["#07121a", "#071a26", "#0b2740"],
            ),
            expand=True,
        )

    def gecmis_sayfasi_olustur():
        kayit_listesi = ft.Column(spacing=8, scroll=ft.ScrollMode.AUTO)
        yukleniyor = ft.Text("Kayitlar yukleniyor...", color=renk_hint, size=13)

        def kayitlari_yukle():
            kayit_listesi.controls.clear()
            kayit_listesi.controls.append(yukleniyor)
            page.update()
            try:
                baglanti = pymysql.connect(**vt_ayarlari, connect_timeout=5)
                imle = baglanti.cursor()
                imle.execute("""
                    SELECT sicaklik_dht, nem, sicaklik_lm35, enlem, boylam, tarih
                    FROM veriler
                    ORDER BY id DESC
                    LIMIT 50
                """)
                kayitlar = imle.fetchall()
                imle.close()
                baglanti.close()

                kayit_listesi.controls.clear()

                if not kayitlar:
                    kayit_listesi.controls.append(
                        ft.Column([
                            ft.Icon(ft.icons.Icons.INBOX, size=50, color="grey"),
                            ft.Text(
                                "Henuz kayit yok",
                                size=15,
                                color="grey",
                                weight=ft.FontWeight.BOLD
                            ),
                            ft.Text(
                                "Analiz yaparak kayit olusturun",
                                size=12,
                                color="lightgrey"
                            ),
                        ],
                            horizontal_alignment=ft.CrossAxisAlignment.CENTER,
                            spacing=8
                        )
                    )
                else:
                    for kayit in kayitlar:
                        sicaklik_dht, nem, sicaklik_lm35, enlem, boylam, tarih = kayit
                        try:
                            sicaklik_dht = float(sicaklik_dht)
                            nem = float(nem)
                            sicaklik_lm35 = float(sicaklik_lm35)
                            enlem = float(enlem)
                            boylam = float(boylam)
                        except (TypeError, ValueError):
                            pass
                        try:
                            dt = datetime.datetime.fromisoformat(str(tarih))
                        except Exception:
                            dt = datetime.datetime.now()
                        tarih_str = dt.strftime("%d.%m.%Y %H:%M")

                        # Sicakliga gore renk belirle
                        if sicaklik_dht <= kritik_sicaklik:
                            kart_bg = renk_koyu_kart
                            sic_color = "#87ceeb"
                            simge = ft.icons.Icons.AC_UNIT
                        elif sicaklik_dht <= 2.0:
                            kart_bg = renk_kart_variant
                            sic_color = "#ff9800"
                            simge = ft.icons.Icons.WARNING
                        else:
                            kart_bg = "#0b2436"
                            sic_color = "#90ee90"
                            simge = ft.icons.Icons.CHECK_CIRCLE

                        kart = ft.Container(
                            content=ft.Column([
                                ft.Row([
                                    ft.Icon(simge, color=sic_color, size=26),
                                    ft.Column([
                                        ft.Text(
                                            tarih_str,
                                            weight=ft.FontWeight.BOLD,
                                            size=13,
                                            color="white"
                                        ),
                                        ft.Text(
                                            f"DHT: {sicaklik_dht:.1f}C  |  "
                                            f"LM35: {sicaklik_lm35:.1f}C  |  "
                                            f"Nem: {nem:.0f}%",
                                            size=11,
                                            color=renk_yazi_acik
                                        ),
                                        ft.Text(
                                            f"Enlem: {enlem:.4f}  |  Boylam: {boylam:.4f}",
                                            size=10,
                                            color=renk_hint
                                        ),
                                    ], spacing=2, expand=True),
                                ], spacing=10),
                            ], spacing=3),
                            padding=12,
                            bgcolor=kart_bg,
                            border_radius=8,
                            margin=ft.margin.symmetric(horizontal=15, vertical=3),
                        )
                        kayit_listesi.controls.append(kart)
                page.update()

            except Exception as e:
                kayit_listesi.controls.clear()
                kayit_listesi.controls.append(
                    ft.Text(
                        f"Veritabani baglanti hatasi: {e}",
                        color="red",
                        size=12
                    )
                )
                page.update()
                print(f"Gecmis yukleme hatasi: {e}")

        kayitlari_yukle()

        return ft.Container(
            content=ft.Column([
                create_appbar(),
                ft.Container(
                    content=ft.Row([
                        ft.IconButton(
                            icon=ft.icons.Icons.ARROW_BACK,
                            on_click=lambda _: page.go("/"),
                            icon_color="white"
                        ),
                        ft.Text(
                            "Gecmis Analiz Sonuclari",
                            size=18,
                            weight=ft.FontWeight.BOLD,
                            color="white"
                        ),
                        ft.IconButton(
                            icon=ft.icons.Icons.REFRESH,
                            on_click=lambda _: kayitlari_yukle(),
                            icon_color="white"
                        ),
                    ], spacing=10),
                    padding=ft.padding.symmetric(horizontal=10, vertical=8),
                    bgcolor="#071a26",
                    width=float('inf'),
                ),
                ft.Column([
                    ft.Container(height=10),
                    kayit_listesi,
                    ft.Container(height=20),
                ], expand=True, scroll=ft.ScrollMode.AUTO),
            ], spacing=0, expand=True),
            gradient=ft.LinearGradient(
                begin=ft.Alignment.TOP_LEFT,
                end=ft.Alignment.BOTTOM_RIGHT,
                colors=["#07121a", "#071a26", "#0b2740"],
            ),
            expand=True,
        )

    def hakkimizda_sayfasi_olustur():
        return ft.Container(
            content=ft.Column([
                create_appbar(),
                ft.Container(
                    content=ft.Row([
                        ft.IconButton(
                            icon=ft.icons.Icons.ARROW_BACK,
                            on_click=lambda _: page.go("/"),
                            icon_color="white"
                        ),
                        ft.Text(
                            "Hakkimizda",
                            size=18,
                            weight=ft.FontWeight.BOLD,
                            color="white"
                        ),
                    ], spacing=10),
                    padding=ft.padding.symmetric(horizontal=10, vertical=8),
                    bgcolor="#071a26",
                    width=float('inf'),
                ),
                ft.Column([
                    ft.Container(height=20),
                    ft.Container(
                        content=ft.Column([
                            ft.Text(
                                "Zirai Don Uyari Sistemi",
                                size=18,
                                weight=ft.FontWeight.BOLD,
                                color="white"
                            ),
                            ft.Divider(color="#2d5a7b"),
                            ft.Text(
                                "Proje Bilgileri",
                                size=14,
                                weight=ft.FontWeight.BOLD,
                                color=renk_acik_mavi
                            ),
                            ft.Container(height=5),
                            ft.Text(
                                "Bu uygulama TUBITAK icin gelistirilmis bir "
                                "tarimsal arastirma projesidir.",
                                size=12,
                                color=renk_yazi_acik
                            ),
                            ft.Container(height=12),
                            ft.Text(
                                "Amac",
                                size=14,
                                weight=ft.FontWeight.BOLD,
                                color=renk_acik_mavi
                            ),
                            ft.Container(height=5),
                            ft.Text(
                                "Ciftcileri don riskinden korumak ve tarimsal "
                                "zararlari minimize etmek icin erken uyari "
                                "sistemi saglamak.",
                                size=12,
                                color=renk_yazi_acik
                            ),
                            ft.Container(height=12),
                            ft.Text(
                                "Teknoloji",
                                size=14,
                                weight=ft.FontWeight.BOLD,
                                color=renk_acik_mavi
                            ),
                            ft.Container(height=5),
                            ft.Text(
                                "Open-Meteo API ile gercek zamanli hava verileri, "
                                "Arduino DHT ve LM35 sensoru ile sicaklik olcumu, "
                                "MySQL ile veri depolama.",
                                size=12,
                                color=renk_yazi_acik
                            ),
                            ft.Container(height=12),
                            ft.Text(
                                "Ozellikler",
                                size=14,
                                weight=ft.FontWeight.BOLD,
                                color=renk_acik_mavi
                            ),
                            ft.Container(height=5),
                            ft.Text(
                                "- IP tabanli konum tespiti\n"
                                "- 3 gunluk saat saat hava tahmini\n"
                                "- Radyasyon ve advektif don analizi\n"
                                "- DHT ve LM35 sensor verileri\n"
                                "- Enlem/Boylam tabanli kayit\n"
                                "- Gecmis kayit goruntumleme",
                                size=12,
                                color=renk_yazi_acik
                            ),
                            ft.Container(height=20),
                        ], spacing=5),
                        padding=20,
                        bgcolor=renk_koyu_kart,
                        border_radius=10,
                        width=360,
                        margin=ft.margin.symmetric(horizontal=15),
                    ),
                    ft.Container(height=20),
                ], expand=True, scroll=ft.ScrollMode.AUTO),
            ], spacing=0, expand=True),
            gradient=ft.LinearGradient(
                begin=ft.Alignment.TOP_LEFT,
                end=ft.Alignment.BOTTOM_RIGHT,
                colors=["#07121a", "#071a26", "#0b2740"],
            ),
            expand=True,
        )

    def rota_degistirme(e):
        route = None
        if e is not None:
            route = getattr(e, 'route', None)
        route = route or page.route or "/"
        print(f"Rota degistirme: {route}")
        page.views.clear()
        if route == "/":
            try:
                sayfa = ana_sayfa_olustur()
                print(f"Ana sayfa olusturuldu: {type(sayfa)}")
                # Container'ın content'ini al ve View'e ekle
                if hasattr(sayfa, 'content'):
                    page.views.append(
                        ft.View(
                            "/",
                            [sayfa.content]
                        )
                    )
                else:
                    page.views.append(
                        ft.View(
                            "/",
                            [sayfa]
                        )
                    )
            except Exception as ex:
                print(f"Ana sayfa hatasi: {ex}")
                import traceback
                traceback.print_exc()
                return
        elif route == "/sonuc":
            page.views.append(
                ft.View(
                    "/sonuc",
                    [sonuc_sayfasi_olustur()],
                    scroll=ft.ScrollMode.AUTO,
                    bgcolor="#07121a"
                )
            )
        elif route == "/gecmis":
            page.views.append(
                ft.View(
                    "/gecmis",
                    [gecmis_sayfasi_olustur()],
                    scroll=ft.ScrollMode.AUTO,
                    bgcolor="#07121a"
                )
            )
        elif route == "/hakkimizda":
            page.views.append(
                ft.View(
                    "/hakkimizda",
                    [hakkimizda_sayfasi_olustur()],
                    scroll=ft.ScrollMode.AUTO,
                    bgcolor="#07121a"
                )
            )
        else:
            page.views.append(
                ft.View(
                    "/",
                    [ana_sayfa_olustur()],
                    scroll=ft.ScrollMode.AUTO,
                    bgcolor="#07121a"
                )
            )
        page.update()

    def goruntu_degistir(sayfa_tipi):
        print(f"Sayfa: {sayfa_tipi}")
        page.clean()
        try:
            if sayfa_tipi == "/":
                sayfa = ana_sayfa_olustur()
            elif sayfa_tipi == "/sonuc":
                sayfa = sonuc_sayfasi_olustur()
            elif sayfa_tipi == "/gecmis":
                sayfa = gecmis_sayfasi_olustur()
            elif sayfa_tipi == "/hakkimizda":
                sayfa = hakkimizda_sayfasi_olustur()
            else:
                sayfa = ana_sayfa_olustur()
            
            if hasattr(sayfa, 'content'):
                page.add(sayfa.content)
            else:
                page.add(sayfa)
            print(f"Sayfa rendered: {sayfa_tipi}")
        except Exception as ex:
            print(f"Sayfa hatasi: {ex}")
            import traceback
            traceback.print_exc()

    def page_go(route):
        print(f"Going to: {route}")
        page.route = route
        goruntu_degistir(route)
    
    # page.go yerine custom page_go kullan - global state'e ekle
    page._custom_go = page_go

    page.route = page.route or "/"
    page.on_route_change = None
    page.on_view_pop = None
    print(f"Page route: {page.route}")
    
    # Başlangıçta ana sayfa göster
    goruntu_degistir("/")






if __name__ == "__main__":
    ft.run(main)
