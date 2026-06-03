#!/usr/bin/env python3
"""Score agent-selection-from-pool recall vs codefirst@8 (gold from /tmp/cand_gold.json)."""
import json, os

SEL = {
 "thrivex-article-list-guest": [
  "common/util/src/main/java/liuyuyang/net/interceptor/JwtTokenAdminInterceptor.java::JwtTokenAdminInterceptor.preHandle",
  "blog/src/main/java/liuyuyang/net/controller/ArticleController.java::ArticleController.getArticleList",
  "blog/src/main/java/liuyuyang/net/service/impl/ArticleServiceImpl.java::ArticleServiceImpl.getArticleList",
  "blog/src/main/java/liuyuyang/net/service/impl/ArticleServiceImpl.java::ArticleServiceImpl.queryWrapperArticle",
  "blog/src/main/java/liuyuyang/net/service/impl/ArticleServiceImpl.java::ArticleServiceImpl.bindingData",
  "blog/src/main/java/liuyuyang/net/controller/ArticleController.java::ArticleController.list",
  "blog/src/main/java/liuyuyang/net/controller/ArticleController.java::ArticleController.paging",
  "blog/src/main/java/liuyuyang/net/service/impl/ArticleServiceImpl.java::ArticleServiceImpl.list",
  "blog/src/main/java/liuyuyang/net/service/impl/ArticleServiceImpl.java::ArticleServiceImpl.paging",
  "common/util/src/main/java/liuyuyang/net/interceptor/JwtTokenAdminInterceptor.java::JwtTokenAdminInterceptor"],
 "kcloud-cache-result": [
  "laokou-common/laokou-common-data-cache/src/main/java/org/laokou/common/data/cache/config/RedisCacheManager.java::RedisCacheManager.createConfig",
  "laokou-common/laokou-common-data-cache/src/main/java/org/laokou/common/data/cache/config/RedisCacheManager.java::RedisCacheManager.createDefaultConfig",
  "laokou-common/laokou-common-data-cache/src/main/java/org/laokou/common/data/cache/config/RedisCacheManager.java::RedisCacheManager",
  "laokou-common/laokou-common-data-cache/src/main/java/org/laokou/common/data/cache/config/DataCacheAutoConfig.java::DataCacheAutoConfig.redisCacheManager",
  "laokou-common/laokou-common-redis/src/main/java/org/laokou/common/redis/config/JacksonCodec.java::JacksonCodec.getJsonMapper",
  "laokou-common/laokou-common-redis/src/main/java/org/laokou/common/redis/config/JacksonCodec.java::JacksonCodec.JacksonCodec",
  "laokou-common/laokou-common-redis/src/main/java/org/laokou/common/redis/config/JacksonCodec.java::JacksonCodec",
  "laokou-common/laokou-common-i18n/src/main/java/org/laokou/common/i18n/dto/Result.java::Result",
  "laokou-common/laokou-common-i18n/src/main/java/org/laokou/common/i18n/dto/Result.java::Result.Result",
  "laokou-common/laokou-common-data-cache/src/main/java/org/laokou/common/data/cache/config/DataCacheAutoConfig.java::DataCacheAutoConfig"],
 "thrivex-comment-email": [
  "blog/src/main/java/liuyuyang/net/controller/CommentController.java::CommentController.add",
  "blog/src/main/java/liuyuyang/net/controller/EmailController.java::EmailController.comment",
  "blog/src/main/java/liuyuyang/net/controller/EmailController.java::EmailController",
  "common/util/src/main/java/liuyuyang/net/utils/EmailUtils.java::EmailUtils.send",
  "common/util/src/main/java/liuyuyang/net/utils/EmailUtils.java::EmailUtils",
  "model/src/main/java/liuyuyang/net/dto/email/CommentEmailDTO.java::CommentEmailDTO",
  "model/src/main/java/liuyuyang/net/model/Comment.java::Comment",
  "blog/src/main/java/liuyuyang/net/controller/CommentController.java::CommentController",
  "model/src/main/java/liuyuyang/net/dto/email/EmailDTO.java::EmailDTO",
  "blog/src/main/java/liuyuyang/net/controller/EmailController.java::EmailController.dismiss"],
 "youlai-currentuser": [
  "src/main/java/com/youlai/boot/system/service/impl/UserServiceImpl.java::UserServiceImpl.getCurrentUserInfo",
  "src/main/java/com/youlai/boot/system/converter/UserConverter.java::UserConverter.toCurrentUserDto",
  "src/main/java/com/youlai/boot/system/model/dto/CurrentUserDTO.java::CurrentUserDTO",
  "src/main/java/com/youlai/boot/system/converter/UserConverter.java::UserConverter",
  "src/main/java/com/youlai/boot/system/controller/UserController.java::UserController.getCurrentUser",
  "src/main/java/com/youlai/boot/system/service/UserService.java::UserService.getCurrentUserInfo",
  "src/main/java/com/youlai/boot/system/service/impl/UserServiceImpl.java::UserServiceImpl",
  "src/main/java/com/youlai/boot/system/model/bo/UserBO.java::UserBO",
  "src/main/java/com/youlai/boot/system/mapper/UserMapper.java::UserMapper.getUserProfile",
  "src/main/java/com/youlai/boot/system/mapper/UserMapper.java::UserMapper"],
 "thrivex-wall-message": [
  "blog/src/main/java/liuyuyang/net/controller/WallController.java::WallController.add",
  "blog/src/main/java/liuyuyang/net/service/impl/WallServiceImpl.java::WallServiceImpl.add",
  "blog/src/main/java/liuyuyang/net/service/WallService.java::WallService.add",
  "blog/src/main/java/liuyuyang/net/controller/EmailController.java::EmailController.comment",
  "blog/src/main/java/liuyuyang/net/controller/EmailController.java::EmailController",
  "common/util/src/main/java/liuyuyang/net/utils/EmailUtils.java::EmailUtils.send",
  "model/src/main/java/liuyuyang/net/model/Wall.java::Wall",
  "model/src/main/java/liuyuyang/net/dto/email/CommentEmailDTO.java::CommentEmailDTO",
  "blog/src/main/java/liuyuyang/net/service/impl/WallServiceImpl.java::WallServiceImpl",
  "common/util/src/main/java/liuyuyang/net/utils/EmailUtils.java::EmailUtils"],
 "thrivex-empty-password-hash": [
  "blog/src/main/java/liuyuyang/net/service/impl/ArticleServiceImpl.java::ArticleServiceImpl.add",
  "blog/src/main/java/liuyuyang/net/service/impl/ArticleServiceImpl.java::ArticleServiceImpl.edit",
  "blog/src/main/java/liuyuyang/net/controller/ArticleController.java::ArticleController.add",
  "blog/src/main/java/liuyuyang/net/controller/ArticleController.java::ArticleController.edit",
  "blog/src/main/java/liuyuyang/net/service/ArticleService.java::ArticleService.add",
  "blog/src/main/java/liuyuyang/net/service/ArticleService.java::ArticleService.edit",
  "model/src/main/java/liuyuyang/net/model/Article.java::Article",
  "blog/src/main/java/liuyuyang/net/service/impl/ArticleServiceImpl.java::ArticleServiceImpl.get",
  "blog/src/main/java/liuyuyang/net/service/ArticleService.java::ArticleService.get",
  "blog/src/main/java/liuyuyang/net/controller/ArticleController.java::ArticleController.get"],
 "thrivex-deleted-article-nav": [
  "blog/src/main/java/liuyuyang/net/service/impl/ArticleServiceImpl.java::ArticleServiceImpl.getRecommendedArticles",
  "blog/src/main/java/liuyuyang/net/controller/ArticleController.java::ArticleController.getRecommendedArticles",
  "blog/src/main/java/liuyuyang/net/service/ArticleService.java::ArticleService.getRecommendedArticles",
  "blog/src/main/java/liuyuyang/net/service/impl/ArticleServiceImpl.java::ArticleServiceImpl.queryWrapperArticle",
  "blog/src/main/java/liuyuyang/net/service/impl/ArticleServiceImpl.java::ArticleServiceImpl.getArticleList",
  "blog/src/main/java/liuyuyang/net/service/impl/ArticleServiceImpl.java::ArticleServiceImpl.del",
  "blog/src/main/java/liuyuyang/net/service/impl/ArticleServiceImpl.java::ArticleServiceImpl.delBatch",
  "blog/src/main/java/liuyuyang/net/service/impl/ArticleServiceImpl.java::ArticleServiceImpl.list",
  "blog/src/main/java/liuyuyang/net/mapper/ArticleMapper.java::ArticleMapper.getArticleList",
  "blog/src/main/java/liuyuyang/net/service/impl/ArticleServiceImpl.java::ArticleServiceImpl.get"],
 "thrivex-category-sort": [
  "blog/src/main/java/liuyuyang/net/service/impl/ArticleServiceImpl.java::ArticleServiceImpl.queryWrapperArticle",
  "blog/src/main/java/liuyuyang/net/service/impl/ArticleServiceImpl.java::ArticleServiceImpl.getArticleList",
  "blog/src/main/java/liuyuyang/net/mapper/ArticleMapper.java::ArticleMapper.getArticleList",
  "blog/src/main/java/liuyuyang/net/mapper/ArticleMapper.java::ArticleMapper.getCateList",
  "model/src/main/java/liuyuyang/net/vo/SortVO.java::SortVO",
  "common/util/src/main/java/liuyuyang/net/utils/YuYangUtils.java::YuYangUtils.queryWrapperFilter",
  "blog/src/main/java/liuyuyang/net/service/impl/ArticleServiceImpl.java::ArticleServiceImpl.list",
  "blog/src/main/java/liuyuyang/net/service/impl/ArticleServiceImpl.java::ArticleServiceImpl.paging",
  "model/src/main/java/liuyuyang/net/vo/article/ArticleFillterVo.java::ArticleFillterVo",
  "blog/src/main/java/liuyuyang/net/controller/ArticleController.java::ArticleController.getArticleList"],
}

meta = json.load(open(os.path.expanduser("/tmp/cand_gold.json")))
fileof = lambda q: q.split("::")[0]
print(f"  {'':30s} {'--- symbol-level ---':>26s}   {'--- file-level ---':>22s}")
print(f"  {'bug':30s} {'cf@8':>7s} {'agent':>7s} {'ceil':>5s}   {'cf@8':>7s} {'agent':>7s} {'ceil':>5s}")
A = {k: [0.0, 0] for k in ("scf", "sag", "sce", "fcf", "fag", "fce")}
for bid, m in meta.items():
    gold = set(m["gold"])
    if not gold:
        continue
    sel = set(SEL.get(bid, []))
    cf = set(m["cf8"])
    gf = {fileof(g) for g in gold}
    def r(a, b): return len(a & b) / len(b) if b else 0.0
    scf, sag, sce = r(cf, gold), r(sel, gold), m["ceiling"]
    fcf = r({fileof(x) for x in cf}, gf)
    fag = r({fileof(x) for x in sel}, gf)
    # file ceiling: assume pool covers what symbol ceiling*gold maps to; approximate as 1.0 if symbol ceiling>0
    fce = 1.0 if m["ceiling"] > 0 else 0.0
    for k, v in (("scf", scf), ("sag", sag), ("sce", sce), ("fcf", fcf), ("fag", fag), ("fce", fce)):
        A[k][0] += v; A[k][1] += 1
    print(f"  {bid:30s} {scf:7.2f} {sag:7.2f} {sce:5.2f}   {fcf:7.2f} {fag:7.2f} {fce:5.2f}")
m = lambda k: A[k][0] / A[k][1]
print(f"\n  {'AVERAGE':30s} {m('scf'):7.2f} {m('sag'):7.2f} {m('sce'):5.2f}   {m('fcf'):7.2f} {m('fag'):7.2f} {m('fce'):5.2f}")
print("\n  cf@8=codefirst heuristic  agent=agent picks from the provenance pool  ceil=pool recall")
