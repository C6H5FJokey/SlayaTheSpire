package spireagent;

import java.lang.reflect.Field;
import java.util.HashMap;
import java.util.Map;

/**
 * 极小的反射助手。
 *
 * 游戏里有些状态字段是 private（商店的遗物/药水列表、篝火按钮列表等），
 * 而模组不能改游戏源码。用反射读它们比依赖 BaseMod 的 `ReflectionHacks`
 * 更稳（后者是 BaseMod 的 API，跨版本可能变）。读不到就返回 null，
 * 由调用方降级 —— **任何反射失败都不允许影响观测主流程**。
 */
public final class Reflect {

    private static final Map<String, Field> CACHE = new HashMap<String, Field>();

    private Reflect() {
    }

    public static Object get(Object target, Class<?> owner, String field) {
        if (target == null) {
            return null;
        }
        try {
            Field f = field(owner, field);
            return f == null ? null : f.get(target);
        } catch (RuntimeException e) {
            return null;
        } catch (IllegalAccessException e) {
            return null;
        }
    }

    public static Object getStatic(Class<?> owner, String field) {
        try {
            Field f = field(owner, field);
            return f == null ? null : f.get(null);
        } catch (RuntimeException e) {
            return null;
        } catch (IllegalAccessException e) {
            return null;
        }
    }

    /** 写（可能是 private 的）字段。失败返回 false，由调用方降级。 */
    public static boolean set(Object target, Class<?> owner, String field, Object value) {
        if (target == null) {
            return false;
        }
        try {
            Field f = field(owner, field);
            if (f == null) {
                return false;
            }
            f.set(target, value);
            return true;
        } catch (RuntimeException e) {
            return false;
        } catch (IllegalAccessException e) {
            return false;
        }
    }

    /**
     * 调用（可能是 private/protected 的）方法。
     *
     * 游戏的"提交点"很多是 protected/private —— 例如事件的 `buttonEffect(int)`、
     * 商店的 `purchaseCard`。模组要复用游戏自身的逻辑而不是模拟鼠标，所以必须
     * 反射调用。失败返回 null，由调用方降级。
     */
    public static Object call(Object target, Class<?> owner, String name,
                              Class<?>[] signature, Object[] args) {
        if (owner == null || name == null) {
            return null;
        }
        java.lang.reflect.Method m = method(owner, name, signature);
        if (m == null) {
            return null;
        }
        try {
            return m.invoke(target, args);
        } catch (RuntimeException e) {
            Log.warn("reflective call " + owner.getSimpleName() + "." + name
                    + " failed: " + e);
            return null;
        } catch (IllegalAccessException e) {
            Log.warn("reflective call " + owner.getSimpleName() + "." + name
                    + " denied: " + e);
            return null;
        } catch (java.lang.reflect.InvocationTargetException e) {
            Log.warn("reflective call " + owner.getSimpleName() + "." + name
                    + " threw: " + e.getCause());
            return null;
        }
    }

    private static java.lang.reflect.Method method(Class<?> owner, String name,
                                                   Class<?>[] signature) {
        Class<?> c = owner;
        while (c != null) {
            try {
                java.lang.reflect.Method m = c.getDeclaredMethod(name, signature);
                m.setAccessible(true);
                return m;
            } catch (NoSuchMethodException e) {
                c = c.getSuperclass();
            } catch (RuntimeException e) {
                return null;
            }
        }
        return null;
    }

    private static Field field(Class<?> owner, String name) {
        if (owner == null || name == null) {
            return null;
        }
        String key = owner.getName() + "#" + name;
        synchronized (CACHE) {
            if (CACHE.containsKey(key)) {
                return CACHE.get(key);
            }
        }
        Field found = null;
        Class<?> c = owner;
        while (c != null && found == null) {
            try {
                found = c.getDeclaredField(name);
            } catch (NoSuchFieldException e) {
                c = c.getSuperclass();
            }
        }
        if (found != null) {
            try {
                found.setAccessible(true);
            } catch (RuntimeException e) {
                found = null;
            }
        }
        synchronized (CACHE) {
            CACHE.put(key, found);
        }
        return found;
    }
}
