/* Flat C ABI around SOEM 2.x for ctypes; hides ecx_contextt layout from Python. */
#include <stdlib.h>
#include <string.h>
#include "soem/soem.h"

#if defined(_WIN32)
#define ECM_API __declspec(dllexport)
#else
#define ECM_API __attribute__((visibility("default")))
#endif

#define ECM_IOMAP_SIZE 4096

typedef struct
{
   ecx_contextt ctx;
   uint8 iomap[ECM_IOMAP_SIZE];
   int sdo_read_timeout;
   int sdo_write_timeout;
   int opened;
   ec_ODlistt odlist;   /* last object dictionary list read (SDO info) */
   ec_OElistt oelist;   /* last object entry list read */
} ecm_master;

typedef struct
{
   char name[EC_MAXNAME + 1];
   uint32 man, id, rev, ser;
   uint16 state, al_status, configadr, aliasadr;
   uint32 obytes, ibytes;
   uint16 obits, ibits;
   uint8 hasdc, coe_details, islost;
   uint16 mbx_proto;
} ecm_slave_info;

typedef struct
{
   int etype;
   uint16 slave, index;
   uint8 subidx;
   int32 abort_code;
   uint16 error_code;
   uint8 error_reg, b1;
   uint16 w1, w2;
} ecm_error;

ECM_API int ecm_version(void) { return 2; }

ECM_API int ecm_find_adapters(char *names, char *descs, int maxn, int len)
{
   ec_adaptert *head = ec_find_adapters(), *a;
   int n = 0;
   for (a = head; a != NULL && n < maxn; a = a->next, n++)
   {
      strncpy(names + n * len, a->name, len - 1);
      strncpy(descs + n * len, a->desc, len - 1);
   }
   ec_free_adapters(head);
   return n;
}

ECM_API ecm_master *ecm_create(void)
{
   ecm_master *m = calloc(1, sizeof(*m));
   if (m)
   {
      m->sdo_read_timeout = EC_TIMEOUTRXM;
      m->sdo_write_timeout = EC_TIMEOUTRXM;
   }
   return m;
}

ECM_API void ecm_destroy(ecm_master *m) { free(m); }
ECM_API int ecm_open(ecm_master *m, const char *ifname)
{
   int rc = ecx_init(&m->ctx, ifname);
   m->opened = rc > 0;
   return rc;
}

/* ecx_close() frees the mailbox mutexes without clearing the pointers, so a
 * second close (or close after a failed scan) is a double free. */
ECM_API void ecm_close(ecm_master *m)
{
   if (!m->opened) return;
   ecx_close(&m->ctx);
   for (int lp = 0; lp < EC_MAXGROUP; lp++) m->ctx.grouplist[lp].mbxtxqueue.mbxmutex = NULL;
   m->ctx.mbxpool.mbxmutex = NULL;
   m->opened = 0;
}
ECM_API int ecm_config_init(ecm_master *m) { return ecx_config_init(&m->ctx); }
ECM_API int ecm_slavecount(ecm_master *m) { return m->ctx.slavecount; }

ECM_API int ecm_config_map(ecm_master *m, int overlap)
{
   (void)overlap; /* SOEM 2.x: overlap is a context flag */
   m->ctx.overlappedMode = overlap ? TRUE : FALSE;
   return ecx_config_map_group(&m->ctx, m->iomap, 0);
}

ECM_API int ecm_config_dc(ecm_master *m) { return ecx_configdc(&m->ctx); }
ECM_API int ecm_expected_wkc(ecm_master *m, int group)
{
   ec_groupt *g = &m->ctx.grouplist[group];
   return g->outputsWKC * 2 + g->inputsWKC;
}

ECM_API void ecm_set_sdo_timeouts(ecm_master *m, int rd_us, int wr_us)
{
   if (rd_us > 0) m->sdo_read_timeout = rd_us;
   if (wr_us > 0) m->sdo_write_timeout = wr_us;
}

/* slave: 0 = master (all slaves), 1..n = slave position (SOEM convention) */
ECM_API int ecm_get_info(ecm_master *m, int slave, ecm_slave_info *o)
{
   ec_slavet *s;
   if (slave < 0 || slave > m->ctx.slavecount) return 0;
   s = &m->ctx.slavelist[slave];
   memset(o, 0, sizeof(*o));
   memcpy(o->name, s->name, EC_MAXNAME);
   o->man = s->eep_man; o->id = s->eep_id; o->rev = s->eep_rev; o->ser = s->eep_ser;
   o->state = s->state; o->al_status = s->ALstatuscode;
   o->configadr = s->configadr; o->aliasadr = s->aliasadr;
   o->obytes = s->Obytes; o->ibytes = s->Ibytes; o->obits = s->Obits; o->ibits = s->Ibits;
   o->hasdc = s->hasdc; o->coe_details = s->CoEdetails; o->islost = s->islost;
   o->mbx_proto = s->mbx_proto;
   return 1;
}

ECM_API uint8 *ecm_outputs(ecm_master *m, int slave) { return m->ctx.slavelist[slave].outputs; }
ECM_API uint8 *ecm_inputs(ecm_master *m, int slave) { return m->ctx.slavelist[slave].inputs; }

ECM_API int ecm_get_state(ecm_master *m, int slave) { return m->ctx.slavelist[slave].state; }
ECM_API void ecm_set_state(ecm_master *m, int slave, int st) { m->ctx.slavelist[slave].state = (uint16)st; }
ECM_API int ecm_read_state(ecm_master *m) { return ecx_readstate(&m->ctx); }
ECM_API int ecm_write_state(ecm_master *m, int slave) { return ecx_writestate(&m->ctx, (uint16)slave); }
ECM_API int ecm_state_check(ecm_master *m, int slave, int st, int timeout_us)
{
   return ecx_statecheck(&m->ctx, (uint16)slave, (uint16)st, timeout_us);
}
ECM_API void ecm_set_islost(ecm_master *m, int slave, int v) { m->ctx.slavelist[slave].islost = v ? TRUE : FALSE; }

ECM_API int ecm_recover(ecm_master *m, int slave, int timeout_us) { return ecx_recover_slave(&m->ctx, (uint16)slave, timeout_us); }
ECM_API int ecm_reconfig(ecm_master *m, int slave, int timeout_us) { return ecx_reconfig_slave(&m->ctx, (uint16)slave, timeout_us); }

ECM_API int ecm_send(ecm_master *m) { return ecx_send_processdata(&m->ctx); }
ECM_API int ecm_receive(ecm_master *m, int timeout_us) { return ecx_receive_processdata(&m->ctx, timeout_us); }

/* SOEM 2.x ecx_mbxsend() refuses to send while the cached slave state is
 * below PRE-OP (SOEM 1.4 did not check). config_init() only requests PRE-OP
 * and nothing refreshes slavelist[].state until read_state(), so every SDO
 * would fail with WKC 0. Refresh the AL status of that slave first. */
static void ecm_refresh_state(ecm_master *m, int slave)
{
   ec_slavet *s = &m->ctx.slavelist[slave];
   uint16 al = 0;
   if ((s->state & 0x0f) >= EC_STATE_PRE_OP) return;
   if (ecx_FPRD(&m->ctx.port, s->configadr, ECT_REG_ALSTAT, sizeof(al), &al, EC_TIMEOUTRET) > 0)
      s->state = etohs(al);
}

ECM_API int ecm_sdo_read(ecm_master *m, int slave, int index, int sub, int ca, uint8 *buf, int *size)
{
   ecm_refresh_state(m, slave);
   return ecx_SDOread(&m->ctx, (uint16)slave, (uint16)index, (uint8)sub, ca ? TRUE : FALSE,
                      size, buf, m->sdo_read_timeout);
}

ECM_API int ecm_sdo_write(ecm_master *m, int slave, int index, int sub, int ca, const uint8 *buf, int size)
{
   ecm_refresh_state(m, slave);
   return ecx_SDOwrite(&m->ctx, (uint16)slave, (uint16)index, (uint8)sub, ca ? TRUE : FALSE,
                       size, buf, m->sdo_write_timeout);
}

ECM_API int ecm_foe_write(ecm_master *m, int slave, const char *filename, uint32 password,
                          const uint8 *buf, int size, int timeout_us)
{
   ecm_refresh_state(m, slave);
   return ecx_FOEwrite(&m->ctx, (uint16)slave, (char *)filename, password, size, (void *)buf, timeout_us);
}

ECM_API int ecm_foe_read(ecm_master *m, int slave, const char *filename, uint32 password,
                         uint8 *buf, int *size, int timeout_us)
{
   ecm_refresh_state(m, slave);
   return ecx_FOEread(&m->ctx, (uint16)slave, (char *)filename, password, size, buf, timeout_us);
}

/* Object dictionary (SDO information service), see pysoem CdefSlave.od */
ECM_API int ecm_od_list(ecm_master *m, int slave)
{
   ecm_refresh_state(m, slave);
   memset(&m->odlist, 0, sizeof(m->odlist));
   if (ecx_readODlist(&m->ctx, (uint16)slave, &m->odlist) <= 0) return -1;
   return m->odlist.Entries;
}

ECM_API int ecm_od_index(ecm_master *m, int item) { return m->odlist.Index[item]; }

ECM_API int ecm_od_desc(ecm_master *m, int item, int *datatype, int *objcode, int *maxsub, char *name)
{
   int wkc = ecx_readODdescription(&m->ctx, (uint16)item, &m->odlist);
   *datatype = m->odlist.DataType[item];
   *objcode = m->odlist.ObjectCode[item];
   *maxsub = m->odlist.MaxSub[item];
   memcpy(name, m->odlist.Name[item], EC_MAXNAME + 1);
   return wkc;
}

ECM_API int ecm_od_entries(ecm_master *m, int item, int *n)
{
   int wkc;
   memset(&m->oelist, 0, sizeof(m->oelist));
   wkc = ecx_readOE(&m->ctx, (uint16)item, &m->odlist, &m->oelist);
   *n = m->oelist.Entries;
   return wkc;
}

ECM_API void ecm_oe_get(ecm_master *m, int i, int *datatype, int *bitlen, int *access, char *name)
{
   *datatype = m->oelist.DataType[i];
   *bitlen = m->oelist.BitLength[i];
   *access = m->oelist.ObjAccess[i];
   memcpy(name, m->oelist.Name[i], EC_MAXNAME + 1);
}

ECM_API int ecm_pop_error(ecm_master *m, ecm_error *o)
{
   ec_errort e;
   if (!ecx_poperror(&m->ctx, &e)) return 0;
   memset(o, 0, sizeof(*o));
   o->etype = (int)e.Etype; o->slave = e.Slave; o->index = e.Index; o->subidx = e.SubIdx;
   o->abort_code = e.AbortCode;
   o->error_code = e.ErrorCode; o->error_reg = e.ErrorReg; o->b1 = e.b1; o->w1 = e.w1; o->w2 = e.w2;
   return 1;
}

ECM_API void ecm_dc_sync0(ecm_master *m, int slave, int act, uint32 cycle_ns, int32 shift_ns)
{
   ecx_dcsync0(&m->ctx, (uint16)slave, act ? TRUE : FALSE, cycle_ns, shift_ns);
}

ECM_API void ecm_dc_sync01(ecm_master *m, int slave, int act, uint32 c0, uint32 c1, int32 shift_ns)
{
   ecx_dcsync01(&m->ctx, (uint16)slave, act ? TRUE : FALSE, c0, c1, shift_ns);
}

ECM_API int ecm_fprd(ecm_master *m, int slave, int ado, uint8 *buf, int len, int timeout_us)
{
   return ecx_FPRD(&m->ctx.port, m->ctx.slavelist[slave].configadr, (uint16)ado, (uint16)len, buf, timeout_us);
}

ECM_API int ecm_fpwr(ecm_master *m, int slave, int ado, const uint8 *buf, int len, int timeout_us)
{
   return ecx_FPWR(&m->ctx.port, m->ctx.slavelist[slave].configadr, (uint16)ado, (uint16)len, (uint8 *)buf, timeout_us);
}
