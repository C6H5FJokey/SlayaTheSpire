package spireagent.obs;

import java.util.ArrayList;
import java.util.List;

import com.megacrit.cardcrawl.dungeons.AbstractDungeon;
import com.megacrit.cardcrawl.rooms.AbstractRoom;
import com.megacrit.cardcrawl.rooms.CampfireUI;
import com.megacrit.cardcrawl.rooms.RestRoom;
import com.megacrit.cardcrawl.ui.campfire.AbstractCampfireOption;

import spireagent.Reflect;

/**
 * 篝火可选项的**唯一定义点**（观测与执行共用）。
 *
 * 按钮列表是 private，只能反射读；名字取自类名（`RestOption` -> `Rest`），
 * 与游戏内语言无关 —— 这对数据集很关键：换语言不应改变语义。
 */
public final class CampfireSlots {

    public static final class Slot {
        public final String name;
        public final Object payload;

        Slot(String name, Object payload) {
            this.name = name;
            this.payload = payload;
        }
    }

    private CampfireSlots() {
    }

    public static List<Slot> list() {
        List<Slot> out = new ArrayList<Slot>();
        AbstractRoom room = AbstractDungeon.getCurrRoom();
        if (!(room instanceof RestRoom)) {
            return out;
        }
        CampfireUI ui = ((RestRoom) room).campfireUI;
        if (ui == null) {
            return out;
        }
        List<?> buttons = Reflect.get(ui, CampfireUI.class, "buttons") instanceof List
                ? (List<?>) Reflect.get(ui, CampfireUI.class, "buttons")
                : new ArrayList<Object>();
        for (Object o : buttons) {
            if (!(o instanceof AbstractCampfireOption)) {
                continue;
            }
            AbstractCampfireOption opt = (AbstractCampfireOption) o;
            out.add(new Slot(nameOf(opt), opt));
        }
        return out;
    }

    public static String nameOf(AbstractCampfireOption opt) {
        String simple = opt.getClass().getSimpleName();
        if (simple.endsWith("Option")) {
            simple = simple.substring(0, simple.length() - "Option".length());
        }
        return simple;
    }
}