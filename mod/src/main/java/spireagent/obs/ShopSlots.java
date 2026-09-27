package spireagent.obs;

import java.util.ArrayList;
import java.util.List;

import com.megacrit.cardcrawl.cards.AbstractCard;
import com.megacrit.cardcrawl.dungeons.AbstractDungeon;
import com.megacrit.cardcrawl.shop.ShopScreen;
import com.megacrit.cardcrawl.shop.StorePotion;
import com.megacrit.cardcrawl.shop.StoreRelic;

import spireagent.Reflect;

/**
 * 商店货架的**唯一定义点**。
 *
 * 观测与执行必须看到同一份槽位表：agent 的 `buy:<index>` 里的 index 就是这个
 * 列表的下标。任何一处改了顺序都会让"模型选的东西"和"实际买的东西"错位，
 * 所以只允许在这里定义（见 docs/06-decision-points.md#shop）。
 */
public final class ShopSlots {

    public static final String KIND_CARD = "card";
    public static final String KIND_COLORLESS = "colorless_card";
    public static final String KIND_RELIC = "relic";
    public static final String KIND_POTION = "potion";
    public static final String KIND_PURGE = "purge";

    public static final class Slot {
        public final String kind;
        public final String id;
        public final int price;
        public final Object payload;

        Slot(String kind, String id, int price, Object payload) {
            this.kind = kind;
            this.id = id;
            this.price = price;
            this.payload = payload;
        }
    }

    private ShopSlots() {
    }

    public static List<Slot> list() {
        List<Slot> out = new ArrayList<Slot>();
        ShopScreen shop = AbstractDungeon.shopScreen;
        if (shop == null) {
            return out;
        }
        addCards(out, shop.coloredCards, KIND_CARD);
        addCards(out, shop.colorlessCards, KIND_COLORLESS);

        List<?> relics = asList(Reflect.get(shop, ShopScreen.class, "relics"));
        for (Object o : relics) {
            if (!(o instanceof StoreRelic)) {
                continue;
            }
            StoreRelic sr = (StoreRelic) o;
            if (sr.relic == null || sr.isPurchased) {
                continue;
            }
            out.add(new Slot(KIND_RELIC, sr.relic.relicId, sr.price, sr));
        }

        List<?> potions = asList(Reflect.get(shop, ShopScreen.class, "potions"));
        for (Object o : potions) {
            if (!(o instanceof StorePotion)) {
                continue;
            }
            StorePotion sp = (StorePotion) o;
            if (sp.potion == null || sp.isPurchased) {
                continue;
            }
            out.add(new Slot(KIND_POTION, sp.potion.ID, sp.price, sp));
        }

        if (shop.purgeAvailable) {
            out.add(new Slot(KIND_PURGE, "purge", ShopScreen.actualPurgeCost, null));
        }
        return out;
    }

    private static void addCards(List<Slot> out, List<AbstractCard> cards, String kind) {
        if (cards == null) {
            return;
        }
        for (AbstractCard c : cards) {
            if (c == null) {
                continue;
            }
            out.add(new Slot(kind, c.cardID, c.price, c));
        }
    }

    private static List<?> asList(Object o) {
        return o instanceof List ? (List<?>) o : new ArrayList<Object>();
    }
}